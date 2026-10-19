"""The launcher's events: one JSON object per line on stdout (RFC-036 section 4.4).

    {"event": "starting", "protocol": 1, "pid": 4120, "mode": "installed", ...}
    {"event": "phase", "phase": "starting"}
    {"event": "phase", "phase": "migrating"}
    {"event": "phase", "phase": "api"}
    {"event": "ready", "url": "http://127.0.0.1:51234", "search": "loading", ...}
    {"event": "child", "name": "job_runner", "state": "unavailable", ...}
    {"event": "stopping", "reason": "requested"}
    {"event": "stopped", "exit_code": 0}

stdout carries these and nothing else; text for people goes to stderr, as the
CLI of RFC-033 section 11 does. Whoever started the launcher reads them to know
when to open the window, and on what.

    event            when, and its fields
    starting         first, with `protocol`, `pid`, `mode`; and `database`,
                     `embedded` or `external`, once the configuration was read
    phase            `initdb`, `starting`, `recovering`, `migrating` (the
                     cluster's phases), then `api` and `job_runner` as each
                     child is created
    ready            once: `/health` answered 200. `url`, `search` (what
                     `/health` said), `indexing` (`running`, `restarting`,
                     `unavailable`)
    already_running  instead of everything else, when another launcher holds
                     the data root: `protocol`, `pid`, `url`, `state`. Exit 0
    child            a child -- `api`, `job_runner`, or the `database` -- is
                     `restarting` (with `exit_code`, `delay`), `running` again,
                     or `unavailable` (with `exit_code`), each with `detail`,
                     the last line it left
    address          the API came back on another port: `url`
    stopping         the stop began: `reason` is `requested`, `session_end`
                     or `failure` (with `detail`)
    stopped          the stop ended: `exit_code`
    failed           the launcher could not start: `detail`, the line. Exit 78

`protocol` changes when an event changes its meaning; a field added does not
change it. Every line is ASCII -- `ensure_ascii` -- so the encoding of the pipe
does not matter to the reader.

Nothing here imports the `Settings` or the logger: `already_running` is written
before the configuration is read, by a process that never reads it (section 8).
"""

from __future__ import annotations

import json
import threading
from enum import StrEnum
from typing import Protocol, TextIO

PROTOCOL = 1


class Event(StrEnum):
    STARTING = "starting"
    PHASE = "phase"
    READY = "ready"
    ALREADY_RUNNING = "already_running"
    CHILD = "child"
    ADDRESS = "address"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class EventSink(Protocol):
    def emit(self, event: Event, **fields: object) -> None:
        """Write one event, now."""


class JsonLines:
    """`EventSink` over a text stream, one line per event, flushed one by one.

    Thread-safe: the stop of a session end is asked from the thread of a
    window (section 7.5). A reader that went away -- the pipe closed -- does
    not stop the launcher: the events were for it, and the stop it surely
    asked for still has to happen.
    """

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def emit(self, event: Event, **fields: object) -> None:
        line = json.dumps({"event": event.value, **fields}, ensure_ascii=True)
        with self._lock:
            try:
                self._stream.write(line + "\n")
                self._stream.flush()
            except (OSError, ValueError):
                pass
