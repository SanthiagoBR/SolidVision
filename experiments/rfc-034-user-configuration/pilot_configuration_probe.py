"""What the configuration does today, before RFC-034 decides anything (pilot).

RFC-034 is written before its implementation, so every number of its own is
`TBM`. This is not one of those numbers. It reads how the code *already*
behaves, on this machine, so that the problem statement of the RFC rests on
what was observed and not on what the roadmap presumed:

    paths          which settings are paths, and which of them are absolute
    stray-env      what a variable another program left in the environment does
    unknown-keys   what a misspelt key in a dotenv file does
    encodings      the encodings a Windows editor writes without being asked
    quoting        backslashes and quotes in a dotenv value that is a path
    local-app-data what the defaults become without `LOCALAPPDATA`
    rotation       two processes, one `RotatingFileHandler`, on Windows
    sources        a `Settings` whose sources leave the environment out
    read-only      whether the code root of an installed app can take a `logs/`
    engine         a connection whose port and password are supplied at connect

Nothing here writes outside a temporary directory, nothing connects to a
database, and the values of the checkout's `.env` are never printed: only its
key names are read, to count the ones `Settings` does not know.

Run it with the project's virtualenv, from anywhere, with the output
redirected next to this script (`*.log` is outside git, like every
experiment's log):

    python experiments/rfc-034-user-configuration/pilot_configuration_probe.py > LOG 2>&1

No new dependency: `ctypes` over `shell32`, `ole32` and `kernel32`, and the
`pydantic-settings`, `SQLAlchemy` and `psycopg` the project already has.
"""

from __future__ import annotations

import ctypes
import datetime
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parents[1]
BACKEND = REPOSITORY / "backend"
sys.path.insert(0, str(BACKEND))

import pydantic_settings  # noqa: E402
import sqlalchemy  # noqa: E402

from app.infrastructure.config.settings import Settings  # noqa: E402

SETTING_NAMES = {name.upper() for name in Settings.model_fields}

KNOWN_FOLDER_SOURCE = '''
import ctypes
from ctypes import wintypes


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def local_app_data_known_folder() -> str:
    """FOLDERID_LocalAppData, asked of the shell rather than read from the environment."""
    folder = GUID(
        0xF1B32785,
        0x6FBA,
        0x4FCF,
        (ctypes.c_ubyte * 8)(0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91),
    )
    shell32 = ctypes.WinDLL("shell32")
    ole32 = ctypes.WinDLL("ole32")
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(GUID),
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.HRESULT
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    pointer = ctypes.c_void_p()
    shell32.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(pointer))
    try:
        return ctypes.wstring_at(pointer.value)
    finally:
        ole32.CoTaskMemFree(pointer)
'''

exec(KNOWN_FOLDER_SOURCE)  # noqa: S102 -- the same source the children run


def say(text: str = "") -> None:
    print(text, flush=True)


def section(name: str, question: str) -> None:
    say()
    say(f"== {name} " + "=" * max(4, 70 - len(name)))
    say(question)
    say()


def scrubbed_environment(**extra: str) -> dict[str, str]:
    """The caller's environment without any variable that names a setting."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in SETTING_NAMES
    }
    environment["PYTHONPATH"] = str(BACKEND)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment.update(extra)
    return environment


def child(
    code: str,
    *arguments: str,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code), *arguments],
        cwd=cwd,
        env=scrubbed_environment() if environment is None else environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def last_lines(text: str, count: int = 3) -> str:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return " / ".join(lines[-count:]) if lines else "(nothing)"


def conditions() -> None:
    say("RFC-034 pilot: what the configuration does today")
    say(f"date            {datetime.datetime.now().isoformat(timespec='seconds')}")
    say(f"platform        {platform.platform()}")
    say(f"python          {sys.version.split()[0]} ({sys.executable})")
    say(f"pydantic-settings {pydantic_settings.VERSION}, SQLAlchemy {sqlalchemy.__version__}")
    say(f"ANSI code page  {ctypes.windll.kernel32.GetACP()}")
    say(f"elevated        {bool(ctypes.windll.shell32.IsUserAnAdmin())}")
    say(f"repository      {REPOSITORY}")
    say(f"settings fields {len(Settings.model_fields)}")


# -- paths --------------------------------------------------------------------

PATHS_CODE = """
    import json
    from pathlib import Path

    from app.infrastructure.config.settings import Settings
    from app.infrastructure.logging import logger

    loaded = Settings(_env_file=None, database_password="p4ss-w0rd-probe")
    rows = {}
    for name in Settings.model_fields:
        value = getattr(loaded, name)
        if isinstance(value, Path) or name.endswith(("_directory", "_path")):
            rows[name] = [type(value).__name__, str(value), Path(value).is_absolute()]
    print(json.dumps({
        "cwd": str(Path.cwd()),
        "env_file": str(Settings.model_config["env_file"]),
        "rows": rows,
        "log_resolved": str(logger._resolve_log_directory()),
        "environment": loaded.environment,
        "url": f"postgresql+psycopg://{loaded.database_user}:***@"
               f"{loaded.database_host}:{loaded.database_port}/{loaded.database_name}",
        "password_in_repr": "p4ss-w0rd-probe" in repr(loaded),
    }))
"""


def probe_paths(work: Path) -> None:
    section(
        "paths",
        "No `.env`, no setting in the environment, from two working directories:\n"
        "which settings are paths, which are absolute, and where the log goes.",
    )
    elsewhere = work / "somewhere else"
    elsewhere.mkdir()
    answers = []
    for cwd in (REPOSITORY, elsewhere):
        result = child(PATHS_CODE, cwd=cwd)
        if result.returncode != 0:
            say(f"  cwd {cwd}: FAILED: {last_lines(result.stderr)}")
            return
        answers.append(json.loads(result.stdout.strip().splitlines()[-1]))
    first, second = answers
    say(f"  .env looked for at      {first['env_file']}")
    for name, (kind, value, absolute) in first["rows"].items():
        say(f"  {name:38} {kind:12} absolute={absolute!s:5} {value}")
    say(f"  log directory, resolved by logger.py, cwd={first['cwd']}")
    say(f"      -> {first['log_resolved']}")
    say(f"  log directory, resolved by logger.py, cwd={second['cwd']}")
    say(f"      -> {second['log_resolved']}")
    same = first["rows"] == second["rows"]
    say(f"  the same answers from both working directories: {same}")
    say(f"  `environment` with no `.env`:   {first['environment']!r}")
    say(f"  `database_url` with no `.env`:  {first['url']}")
    say(f"  the password is in repr(Settings): {first['password_in_repr']}")


# -- stray environment --------------------------------------------------------

STRAY_CODE = """
    import sys

    from app.infrastructure.config.settings import settings

    print(repr(getattr(settings, sys.argv[1])))
"""


def probe_stray_environment() -> None:
    section(
        "stray-env",
        "One variable another program left in the user's environment, and then\n"
        "`from app.infrastructure.config.settings import settings`.",
    )
    cases = [
        ("DEBUG", "*", "debug"),
        ("DEBUG", "release", "debug"),
        ("DEBUG", "1", "debug"),
        ("BATCH_SIZE", "auto", "batch_size"),
        ("ENVIRONMENT", " Production ", "environment"),
    ]
    for name, value, field in cases:
        environment = scrubbed_environment(**{name: value})
        result = child(STRAY_CODE, field, environment=environment)
        if result.returncode == 0:
            say(f"  {name}={value!r:14} imports; settings.{field} = {result.stdout.strip()}")
        else:
            say(f"  {name}={value!r:14} DOES NOT IMPORT (exit {result.returncode})")
            say(f"      {last_lines(result.stderr, 4)}")


# -- unknown keys -------------------------------------------------------------

UNKNOWN_CODE = """
    import sys
    import warnings

    from app.infrastructure.config.settings import Settings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        loaded = Settings(_env_file=sys.argv[1])
    print(loaded.batch_size, loaded.thumbnail_max_edge, len(caught))
"""


def probe_unknown_keys(work: Path) -> None:
    section(
        "unknown-keys",
        "A dotenv file whose keys are almost the right ones: EMBEDDING_BATCH_SIZE\n"
        "for BATCH_SIZE, THUMBNAIL_SIZE for THUMBNAIL_MAX_EDGE.",
    )
    file = work / "almost.env"
    file.write_text("EMBEDDING_BATCH_SIZE=64\nTHUMBNAIL_SIZE=256\n", encoding="utf-8")
    result = child(UNKNOWN_CODE, str(file))
    if result.returncode != 0:
        say(f"  FAILED: {last_lines(result.stderr)}")
    else:
        batch, edge, warned = result.stdout.split()
        say(f"  exit 0; batch_size = {batch} (default 8), thumbnail_max_edge = {edge}")
        say(f"  (default 512); warnings raised: {warned}; stderr: {last_lines(result.stderr)}")

    dotenv = REPOSITORY / ".env"
    if not dotenv.is_file():
        say("  this checkout has no .env")
        return
    names = []
    for line in dotenv.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            names.append(stripped.split("=", 1)[0].strip().removeprefix("export "))
    unknown = [name for name in names if name.upper() not in SETTING_NAMES]
    say(f"  this checkout's .env (names only): {len(names)} keys, {len(unknown)} of them")
    say(f"  not a field of Settings: {', '.join(unknown) if unknown else '(none)'}")


# -- encodings ----------------------------------------------------------------

ENCODING_CODE = """
    import sys

    from app.infrastructure.config.settings import Settings

    try:
        loaded = Settings(_env_file=sys.argv[1], _env_file_encoding=sys.argv[2])
    except Exception as error:
        print(f"{type(error).__name__}: {str(error).splitlines()[0][:110]}")
    else:
        print(f"log_level={loaded.log_level!r} thumbnail_directory={str(loaded.thumbnail_directory)!r}")
"""


def probe_encodings(work: Path) -> None:
    section(
        "encodings",
        "The same two lines, LOG_LEVEL=DEBUG first, saved the ways a Windows editor\n"
        "saves them, and read as `utf-8` (what the project reads) and `utf-8-sig`.\n"
        "The default log_level is 'INFO'.",
    )
    content = "LOG_LEVEL=DEBUG\nTHUMBNAIL_DIRECTORY=C:\\Users\\João\\thumbs\n"
    written = {
        "utf-8, no BOM": "utf-8",
        "utf-8 with BOM": "utf-8-sig",
        "utf-16 LE with BOM": "utf-16",
        "cp1252 (ANSI)": "cp1252",
    }
    for label, encoding in written.items():
        file = work / f"encoded-{encoding}.env"
        file.write_text(content, encoding=encoding)
        for read_as in ("utf-8", "utf-8-sig"):
            result = child(ENCODING_CODE, str(file), read_as)
            answer = result.stdout.strip() or last_lines(result.stderr)
            say(f"  saved as {label:20} read as {read_as:10} -> {answer}")


# -- quoting ------------------------------------------------------------------

QUOTING_CODE = """
    import sys

    from app.infrastructure.config.settings import Settings

    loaded = Settings(_env_file=sys.argv[1])
    print(ascii(str(loaded.thumbnail_directory)))
"""


def probe_quoting(work: Path) -> None:
    section(
        "quoting",
        "THUMBNAIL_DIRECTORY, as a Windows user would type it. What reaches the\n"
        "setting, shown with ascii() so that a control character is visible.",
    )
    lines = [
        r"THUMBNAIL_DIRECTORY=D:\fotos\raw\thumbs",
        r'THUMBNAIL_DIRECTORY="D:\fotos\raw\thumbs"',
        r"THUMBNAIL_DIRECTORY='D:\fotos\raw\thumbs'",
        r"THUMBNAIL_DIRECTORY=D:\Meus thumbs\novos",
        r'THUMBNAIL_DIRECTORY="D:\Meus thumbs\novos"',
        r"THUMBNAIL_DIRECTORY=D:\thumbs # o disco grande",
        r"THUMBNAIL_DIRECTORY=%LOCALAPPDATA%\thumbs",
        r"THUMBNAIL_DIRECTORY=${LOCALAPPDATA}\thumbs",
        r"THUMBNAIL_DIRECTORY=thumbs",
    ]
    for index, line in enumerate(lines):
        file = work / f"quoting-{index}.env"
        file.write_text(line + "\n", encoding="utf-8")
        result = child(QUOTING_CODE, str(file))
        answer = result.stdout.strip() or last_lines(result.stderr)
        say(f"  {line}")
        say(f"      -> {answer}")


# -- LOCALAPPDATA -------------------------------------------------------------

LOCAL_APP_DATA_CODE = (
    KNOWN_FOLDER_SOURCE
    + """
import os

from app.infrastructure.config.settings import Settings

loaded = Settings(_env_file=None)
print("LOCALAPPDATA in the environment:", os.environ.get("LOCALAPPDATA"))
print("thumbnail_directory:", loaded.thumbnail_directory)
print("embedded_postgres_directory:", loaded.embedded_postgres_directory)
print("known folder:", local_app_data_known_folder())
"""
)


def probe_local_app_data() -> None:
    section(
        "local-app-data",
        "A child whose environment was built without LOCALAPPDATA, as a launcher\n"
        "that assembles its children's environment could build it.",
    )
    from_environment = os.environ.get("LOCALAPPDATA")
    from_shell = local_app_data_known_folder()  # type: ignore[name-defined]  # noqa: F821
    say(f"  here, from the environment: {from_environment}")
    say(f"  here, from the shell:       {from_shell}")
    started = time.perf_counter()
    for _ in range(1000):
        local_app_data_known_folder()  # type: ignore[name-defined]  # noqa: F821
    elapsed = time.perf_counter() - started
    say(f"  SHGetKnownFolderPath, 1000 calls: {elapsed * 1000:.1f} ms")
    environment = scrubbed_environment()
    environment.pop("LOCALAPPDATA", None)
    result = subprocess.run(
        [sys.executable, "-c", LOCAL_APP_DATA_CODE],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    say("  in a child without the variable:")
    for line in (result.stdout or last_lines(result.stderr)).splitlines():
        say(f"      {line}")


# -- rotation -----------------------------------------------------------------

WRITER_CODE = """
    import logging
    import sys
    import time
    from logging.handlers import RotatingFileHandler

    path, tag, count, pause = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
    # The project's handler (logging/handlers.py), with a smaller file so that
    # the rollover comes in seconds instead of after 10 MB.
    handler = RotatingFileHandler(path, maxBytes=20_000, backupCount=5, encoding="utf-8")
    log = logging.getLogger("probe")
    log.setLevel(logging.INFO)
    log.propagate = False
    log.addHandler(handler)
    for index in range(count):
        log.info("%s-%05d %s", tag, index, "x" * 80)
        if pause:
            time.sleep(pause)
"""

ROTATION_RECORDS = 600


def _count_records(directory: Path, tag: str) -> int:
    found = set()
    for file in directory.glob("application.log*"):
        for line in file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(f"{tag}-"):
                found.add(line.split(" ", 1)[0])
    return len(found)


def _sizes(directory: Path) -> str:
    files = sorted(directory.glob("application.log*"))
    return ", ".join(f"{file.name} {file.stat().st_size}" for file in files)


def _seed_backups(directory: Path) -> None:
    """Five older backups, as a log that has been rotating for a while has."""
    for index in range(1, 6):
        (directory / f"application.log.{index}").write_text(
            f"SEED-{index} an older backup\n", encoding="utf-8"
        )


def _seeds_left(directory: Path) -> str:
    left = []
    for file in sorted(directory.glob("application.log*")):
        text = file.read_text(encoding="utf-8", errors="replace")
        left.extend(
            line.split(" ", 1)[0] for line in text.splitlines() if line.startswith("SEED-")
        )
    return f"{len(left)} of 5 ({', '.join(left) if left else 'none'})"


def probe_rotation(work: Path) -> None:
    section(
        "rotation",
        "The project's RotatingFileHandler with maxBytes=20000 and backupCount=5.\n"
        f"A busy writer logs {ROTATION_RECORDS} records of ~100 bytes, which is ~3 files:\n"
        "none of its own records is rotated away. Each directory starts with\n"
        "five older backups, SEED-1 (newest) to SEED-5. First the writer alone;\n"
        "then while a quiet process that logs twice a second holds the same\n"
        "file; then alone again, while `Get-Content -Wait` follows the file.",
    )
    writer = [sys.executable, "-c", textwrap.dedent(WRITER_CODE)]
    environment = scrubbed_environment()

    alone = work / "rotation-alone"
    alone.mkdir()
    _seed_backups(alone)
    control = subprocess.run(
        [*writer, str(alone / "application.log"), "B", str(ROTATION_RECORDS), "0"],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    say("  control, one process:")
    say(f"      records kept: {_count_records(alone, 'B')} of {ROTATION_RECORDS}")
    say(f"      logging errors on stderr: {control.stderr.count('--- Logging error ---')}")
    say(f"      older backups left: {_seeds_left(alone)}")
    say(f"      files: {_sizes(alone)}")

    shared = work / "rotation-shared"
    shared.mkdir()
    _seed_backups(shared)
    target = str(shared / "application.log")
    quiet = subprocess.Popen(
        [*writer, target, "A", "16", "0.5"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    time.sleep(1.0)
    busy = subprocess.run(
        [*writer, target, "B", str(ROTATION_RECORDS), "0"],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    _, quiet_stderr = quiet.communicate(timeout=120)
    say("  two processes, one file:")
    say(f"      busy writer, records kept: {_count_records(shared, 'B')} of {ROTATION_RECORDS}")
    say(f"      busy writer, logging errors: {busy.stderr.count('--- Logging error ---')}")
    say(f"      quiet writer, records kept: {_count_records(shared, 'A')} of 16")
    say(f"      quiet writer, logging errors: {quiet_stderr.count('--- Logging error ---')}")
    errors = [
        line.strip()
        for line in (busy.stderr + quiet_stderr).splitlines()
        if "Error" in line and "Logging error" not in line
    ]
    say(f"      the error, as logging printed it: {errors[0] if errors else '(none)'}")
    say(f"      older backups left: {_seeds_left(shared)}")
    say(f"      files: {_sizes(shared)}")

    followed = work / "rotation-followed"
    followed.mkdir()
    _seed_backups(followed)
    target = str(followed / "application.log")
    subprocess.run([*writer, target, "A", "1", "0"], env=environment, timeout=120)
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    powershell = Path(system_root) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    reader = subprocess.Popen(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"Get-Content -LiteralPath '{target}' -Wait",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    time.sleep(8.0)
    try:
        busy = subprocess.run(
            [*writer, target, "B", str(ROTATION_RECORDS), "0"],
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    finally:
        reader.kill()
        reader.wait(timeout=30)
    say("  one process, and `Get-Content -Wait` following the file:")
    say(f"      records kept: {_count_records(followed, 'B')} of {ROTATION_RECORDS}")
    say(f"      logging errors on stderr: {busy.stderr.count('--- Logging error ---')}")
    say(f"      older backups left: {_seeds_left(followed)}")
    say(f"      files: {_sizes(followed)}")


# -- sources ------------------------------------------------------------------

SOURCES_CODE = """
    import os
    import sys

    from app.infrastructure.config.settings import Settings

    # Set after the import, not before: with DEBUG=* already in the
    # environment the module does not import at all (see stray-env), because
    # it builds `settings = Settings()` on the way in.
    os.environ["DEBUG"] = "*"
    os.environ["LOG_LEVEL"] = "ERROR"


    class WithoutTheEnvironment(Settings):
        @classmethod
        def settings_customise_sources(
            cls, settings_cls, init_settings, env_settings, dotenv_settings,
            file_secret_settings,
        ):
            return (init_settings, dotenv_settings)


    loaded = WithoutTheEnvironment(_env_file=sys.argv[1])
    print(f"debug={loaded.debug} log_level={loaded.log_level!r} batch_size={loaded.batch_size}")
"""


def probe_sources(work: Path) -> None:
    section(
        "sources",
        "A subclass whose sources are (init, dotenv), without the environment.\n"
        "The environment has DEBUG=*, which does not parse, and LOG_LEVEL=ERROR.\n"
        "The file has LOG_LEVEL=WARNING.",
    )
    file = work / "sources.env"
    file.write_text("LOG_LEVEL=WARNING\n", encoding="utf-8")
    result = child(SOURCES_CODE, str(file))
    say(f"  exit {result.returncode}: {result.stdout.strip() or last_lines(result.stderr, 4)}")


# -- read-only code root ------------------------------------------------------


MKDTEMP_CODE = """
    import sys
    import tempfile

    print(tempfile.mkdtemp(prefix="solidvision-pilot-", dir=sys.argv[1]))
"""


def probe_read_only_root() -> None:
    section(
        "read-only",
        "logger.py anchors a relative log directory at the code root. Installed,\n"
        "that is under Program Files. Can this process make a directory there?\n"
        "Asked twice: with os.mkdir, and with tempfile.mkdtemp.",
    )
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    target = Path(program_files) / f"solidvision-pilot-{os.getpid()}"
    try:
        os.mkdir(target)
    except OSError as error:
        say(f"  os.mkdir under {program_files}: {type(error).__name__}: {error.strerror}")
    else:
        target.rmdir()
        say(f"  os.mkdir under {program_files}: allowed (is this process elevated?)")
        return

    # The first version of this probe used `tempfile.mkdtemp` and never came
    # back. It runs in a child, with a deadline, so that this one does.
    started = time.perf_counter()
    try:
        result = child(MKDTEMP_CODE, program_files, timeout=10.0)
    except subprocess.TimeoutExpired:
        say("  tempfile.mkdtemp under it: no error and no directory after 10 s;")
        say("      the probe stopped waiting")
    else:
        elapsed = time.perf_counter() - started
        made = result.stdout.strip()
        if made:
            shutil.rmtree(made, ignore_errors=True)
        say(f"  tempfile.mkdtemp under it, {elapsed:.1f} s: {made or last_lines(result.stderr, 1)}")


# -- engine -------------------------------------------------------------------

REFUSAL_CODE = """
    import psycopg
    import sqlalchemy
    from sqlalchemy import create_engine, event
    from sqlalchemy.engine import URL

    engine = create_engine(URL.create(
        "postgresql+psycopg", username="solidvision", host="127.0.0.1",
        database="solidvision",
    ))


    @event.listens_for(engine, "do_connect")
    def provide(dialect, record, cargs, cparams):
        raise psycopg.OperationalError("the embedded cluster is not running")


    try:
        engine.connect()
    except Exception as error:
        kind = type(error)
        print(f"{kind.__module__}.{kind.__name__}; "
              f"a SQLAlchemyError: {isinstance(error, sqlalchemy.exc.SQLAlchemyError)}")
"""

SUPPLIED_CODE = """
    import sys
    import time

    from sqlalchemy import create_engine, event
    from sqlalchemy.engine import URL

    port = int(sys.argv[1])
    arguments = {"connect_timeout": int(sys.argv[2])} if sys.argv[2] != "none" else {}
    engine = create_engine(
        URL.create("postgresql+psycopg", username="solidvision", host="127.0.0.1",
                   database="solidvision"),
        connect_args=arguments,
    )
    print("the URL the engine was built from:", engine.url.render_as_string())


    @event.listens_for(engine, "do_connect")
    def provide(dialect, record, cargs, cparams):
        cparams["port"] = port
        cparams["password"] = "supplied-at-connect"


    started = time.perf_counter()
    try:
        engine.connect()
    except Exception as error:
        elapsed = time.perf_counter() - started
        text = " ".join(str(error).split())
        print(f"refused after {elapsed:.1f} s: {text[:150]}")
"""


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def probe_engine() -> None:
    section(
        "engine",
        "An engine built without a port and without a password. A `do_connect`\n"
        "listener supplies both when a connection is opened. No server is used:\n"
        "the port is one that nothing listens on.",
    )
    result = child(REFUSAL_CODE)
    say("  the listener raises psycopg.OperationalError; engine.connect() raises:")
    say(f"      {result.stdout.strip() or last_lines(result.stderr)}")

    # Does the driver go where the listener says? A socket of this probe
    # listens, the listener is given its port, and the socket waits for a
    # caller. It is not a PostgreSQL server: it reads the first bytes and
    # hangs up.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(30.0)
        listening = int(server.getsockname()[1])
        caller = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(SUPPLIED_CODE), str(listening), "3"],
            env=scrubbed_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            accepted, _ = server.accept()
        except TimeoutError:
            say(f"  a socket listening on port {listening}: nobody called in 30 s")
        else:
            with accepted:
                accepted.settimeout(5.0)
                try:
                    first = accepted.recv(8)
                except OSError:
                    first = b""
            say(f"  a socket listening on port {listening}, the port the listener supplies:")
            say(f"      the driver called it, and sent {len(first)} bytes before it hung up")
        caller.communicate(timeout=60)

    port = _closed_port()
    say(f"  the listener supplies port {port}, where nothing listens:")
    for timeout, wait in (("3", 60.0), ("none", 25.0)):
        label = f"connect_timeout={timeout}"
        try:
            result = child(SUPPLIED_CODE, str(port), timeout, timeout=wait)
        except subprocess.TimeoutExpired as expired:
            output = expired.stdout or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            for line in output.splitlines():
                say(f"      {label}: {line}")
            say(f"      {label}: still trying after {wait:.0f} s; the probe stopped waiting")
        else:
            for line in (result.stdout or last_lines(result.stderr)).splitlines():
                say(f"      {label}: {line}")


def main() -> int:
    conditions()
    work = Path(tempfile.mkdtemp(prefix="rfc034-pilot-"))
    try:
        probe_paths(work)
        probe_stray_environment()
        probe_unknown_keys(work)
        probe_encodings(work)
        probe_quoting(work)
        probe_local_app_data()
        probe_rotation(work)
        probe_sources(work)
        probe_read_only_root()
        probe_engine()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    say()
    say("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
