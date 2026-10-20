r"""The API as a program of its own, the way the launcher runs it (RFC-036 5.5).

    python -X utf8 -m app.presentation.api --port 51234 --supervised

uvicorn's `Server`, with the application already imported, on `127.0.0.1` and
the port given. The address is not an argument: the API serves loopback only,
and the second guard of RFC-030 section 6 starts from that.

With `--supervised`, the end of stdin is a Ctrl+C (section 7.1): uvicorn stops
accepting, gives the requests in flight `GRACEFUL_SHUTDOWN_SECONDS`, and shuts
the lifespan down. Its own handler receives the signal, and once it has shut
down it raises the signal again (`capture_signals`), which arrives here as the
`KeyboardInterrupt` that `uvicorn.main.run` also swallows.

Exit codes: 0 after a stop; 3, uvicorn's `STARTUP_FAILURE`, when the port was
taken before uvicorn could bind it -- the launcher's cue to try another
(section 5.4); 78 when the configuration is invalid (section 6.1).

The log is `api.log`, by the rule of RFC-034 section 8.2. `python -m uvicorn
app.presentation.api:app` still works, writes `uvicorn.log`, and is what to run
for `--reload` (section 9).

**No access log** (RFC-037 section 5.3). uvicorn writes a line per request to
stdout, the launcher passes it to its stderr, and the window writes that to its
log: with the interface served here, every thumbnail, every file of the
interface and every `/health` of the polling. The run by hand above keeps it,
for whoever is developing the API.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

import uvicorn

from app.infrastructure.process.supervision import SUPERVISED_FLAG, stop_on_stdin_eof
from app.presentation.api import app

LOOPBACK = "127.0.0.1"

GRACEFUL_SHUTDOWN_SECONDS = 5
"""What the requests in flight get once a stop is asked (section 7.2).

The launcher waits 15 s for the whole API: this, the lifespan, and the margin.
"""


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"{value} is not a TCP port")
    return port


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.presentation.api",
        description="Serve the SolidVision API on 127.0.0.1 (RFC-036 section 5.5).",
    )
    parser.add_argument("--port", type=_port, required=True, help="The TCP port.")
    parser.add_argument(
        SUPERVISED_FLAG,
        action="store_true",
        help="Stop as Ctrl+C would when stdin reaches its end.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_arg_parser().parse_args(argv)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=LOOPBACK,
            port=arguments.port,
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
            access_log=False,
        )
    )
    try:
        if arguments.supervised:
            stop_on_stdin_eof()
        server.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
