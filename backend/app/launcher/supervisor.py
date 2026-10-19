r"""The supervisor: the order of the start, ready, restarts and the stop (RFC-036).

One thread, one loop, a tick of 100 ms. Everything that touches the world --
the children, the cluster, the clock, the `/health` probe, the ports, the
events and the state file -- comes in through the constructor, so that the
tests of section 10 run the whole machine against doubles and a clock that
only moves when the machine sleeps.

The start (section 5.1, with the second branch that section 16 made enter)::

    the port of the API (5.4), and the API -- after the cluster on a first run
    the cluster and the migrations, when the source is embedded, meanwhile
    the job runner
    ready: the first 200 of /health, asked every tick

What the supervision does when something exits (6.1, 6.2)::

    exit                               the supervisor
    during the stop                    nothing: it is expected
    78, at any moment                  no restart; `child` with the last line.
                                       The API: stop everything, 78. The job
                                       runner: carry on, indexing unavailable
    3 from the API before it answered  the port was taken: another port
    anything else                      restart, after the wait of the turn
    the postmaster (status, every 2 s) start() again, under the same limit

The stop (7.2): `stopping`; stdin closed on both children at once; each
waited for up to its deadline, then `TerminateProcess`; the cluster last, and
only when a launcher started it (7.3); `stopped`. A second request during the
stop skips the deadlines.

**Requests to stop are honoured at safe points, never in the middle of a
call.** They arrive as counts, polled every tick: a Ctrl+C or the end of stdin
through the signal handler of `__main__`, a `stop` through the named event, the
end of the session through its window. An `initdb` or a crash recovery is not
cut in half by any of them: the stop begins when the call returns.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple, Protocol

from app.infrastructure.config.constants import EXIT_CANNOT_START
from app.infrastructure.database.embedded.cluster import (
    ClusterReport,
    ClusterStatus,
    Phase,
    StartedCluster,
    StopOutcome,
)
from app.infrastructure.database.embedded.database_setup import (
    ClusterConnection,
    PreparedDatabase,
)
from app.infrastructure.database.embedded.errors import (
    ClusterRefusedError,
    EmbeddedPostgresError,
    RuntimeInvalidError,
)
from app.infrastructure.logging.logger import get_logger
from app.infrastructure.process.supervision import SUPERVISED_FLAG

from .children import API, JOB_RUNNER, Child, Program, last_line
from .events import Event, EventSink
from .instance import ClusterArrival, InstanceRecord, LauncherState, StopRequests

logger = get_logger(__name__)

LOOPBACK = "127.0.0.1"

UVICORN_STARTUP_FAILURE = 3
"""`STARTUP_FAILURE`, in `uvicorn/config.py`: what uvicorn exits with when it
cannot bind its port. Spelt here because the launcher imports no uvicorn."""

DATABASE = "database"
"""The `name` of the cluster in a `child` event."""

PROBE_SECONDS = 2.0
"""How long one `/health` probe waits for its answer."""


@dataclass(frozen=True)
class Timings:
    """Every wait of the supervisor. Choices, unless the docstring says measured."""

    tick: float = 0.1
    """The loop's period, and the period of the `/health` probe while starting
    (section 5.1)."""
    ready_deadline: float = 120.0
    """From the creation of the API to its first 200, before `failed`.

    A database that never answers -- the Docker container of a checkout,
    stopped -- is what it is for. The value is set against the measurement of
    section 16, in `experiments/rfc-036-launcher/measure_launcher.py`."""
    cluster_check: float = 2.0
    """How often `status()` is asked whether there is a server (section 6.2)."""
    restart_probe: float = 0.5
    """How often a restarted API is asked whether it answers yet."""
    api_stop: float = 15.0
    """The API's deadline in the stop: uvicorn's 5 s for the requests in
    flight, the lifespan, and the margin (section 7.2)."""
    job_runner_stop: float = 30.0
    """The job runner's deadline in the stop: the `release()` of the job."""
    session_api_stop: float = 3.0
    session_job_runner_stop: float = 10.0
    """The deadlines at the end of the session (section 7.5), shorter, because
    the system's patience is."""
    restart_delays: tuple[float, ...] = (0.0, 2.0, 5.0, 15.0, 30.0)
    """The wait before each restart in a row (section 6.1)."""
    restart_window: float = 600.0
    restart_limit: int = 5
    """Five exits in ten minutes, and the supervisor gives up on that child as
    if it had exited with 78."""
    stable_after: float = 600.0
    """Up this long, and the next exit waits zero again."""
    port_attempts: int = 3
    """Starts of the API in a row refused by `bind`: RFC-033's number."""


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class Cluster(Protocol):
    """What the supervisor asks of the cluster: `EmbeddedPostgres`, structurally."""

    def start(
        self, on_phase: Callable[[Phase], None] | None = None
    ) -> StartedCluster: ...

    def prepare(
        self,
        connection: ClusterConnection,
        on_phase: Callable[[Phase], None] | None = None,
    ) -> PreparedDatabase: ...

    def status(self) -> ClusterReport: ...

    def stop(self) -> StopOutcome: ...


class Prober(Protocol):
    """`PortProber` of the cluster adapter, structurally (section 5.4)."""

    def usable(self, port: int) -> bool: ...

    def free_port(self) -> int: ...


class StateWriter(Protocol):
    def write(self, record: InstanceRecord) -> bool: ...


@dataclass(frozen=True)
class HealthAnswer:
    status: int
    search: str | None


HealthProbe = Callable[[int], HealthAnswer | None]
"""`GET /health` on a port of `127.0.0.1`: the answer, or `None` for none."""

Spawn = Callable[[Program, Sequence[str]], Child]
"""Create a child of `program` with these arguments, started."""


def probe_health(port: int) -> HealthAnswer | None:
    """One `GET /health`. A refused connection is `None`: "not yet"."""
    connection = http.client.HTTPConnection(LOOPBACK, port, timeout=PROBE_SECONDS)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        body = response.read()
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()
    search: str | None = None
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("search"), str):
        search = payload["search"]
    return HealthAnswer(response.status, search)


class PortRefusedError(Exception):
    """`API_PORT` is set, and that port cannot be used."""


class ApiPorts:
    """The port of the API (section 5.4).

    1. The port of the last start that reached `ready`, if an exclusive
       `bind` accepts it. 2. Otherwise one the system gives. `API_PORT` fixes
    it: that port or a refusal, never another one, because a fixed port is a
    promise to somebody.
    """

    def __init__(self, prober: Prober, *, fixed: int | None, last: int | None) -> None:
        self._prober = prober
        self._fixed = fixed
        self._last = last

    def first(self) -> int:
        if self._fixed is not None:
            if not self._prober.usable(self._fixed):
                raise PortRefusedError(self._taken(self._fixed))
            return self._fixed
        if self._last is not None and self._prober.usable(self._last):
            return self._last
        return self._prober.free_port()

    def another(self, taken: int) -> int:
        """The API exited with 3: `taken` went to somebody else meanwhile."""
        if self._fixed is not None:
            raise PortRefusedError(self._taken(self._fixed))
        return self._prober.free_port()

    def again(self, current: int | None) -> int:
        """For a restart: the same port first, so the window keeps its URL."""
        if self._fixed is not None:
            return self._fixed
        if current is not None and self._prober.usable(current):
            return current
        return self._prober.free_port()

    @staticmethod
    def _taken(port: int) -> str:
        return (
            f"API_PORT is {port}, and that port cannot be used on {LOOPBACK}: "
            "another program holds it, or Windows reserved it. Free it, or remove "
            "API_PORT and SolidVision chooses a port."
        )


class RestartPolicy:
    """Section 6.1: the wait grows, and five exits in ten minutes is the end."""

    def __init__(self, timings: Timings) -> None:
        self._timings = timings
        self._exits: list[float] = []
        self._in_a_row = 0

    def after_exit(self, now: float, started_at: float) -> float | None:
        """The wait before the next start, or `None` to give up."""
        if now - started_at >= self._timings.stable_after:
            self._in_a_row = 0
        window = self._timings.restart_window
        self._exits = [moment for moment in self._exits if now - moment < window]
        self._exits.append(now)
        if len(self._exits) >= self._timings.restart_limit:
            return None
        delays = self._timings.restart_delays
        delay = delays[min(self._in_a_row, len(delays) - 1)]
        self._in_a_row += 1
        return delay


class StopReason(StrEnum):
    REQUESTED = "requested"
    SESSION_END = "session_end"
    FAILURE = "failure"


class _Ending(NamedTuple):
    code: int
    reason: StopReason
    detail: str | None = None


class _StartRefusedError(Exception):
    """The start cannot go on: `failed`, with this line."""

    def __init__(self, line: str, code: int = EXIT_CANNOT_START) -> None:
        super().__init__(line)
        self.line = line
        self.code = code


@dataclass
class _Supervised:
    """One program under supervision, across the children it has been."""

    program: Program
    restarts: RestartPolicy
    child: Child | None = None
    started_at: float = 0.0
    restart_at: float | None = None
    unavailable: bool = False
    port: int | None = None
    answering: bool = False
    """The API answered something on `port` in this incarnation: it has bound
    the port, and an exit 3 can no longer mean the port was taken."""
    port_failures: int = 0
    next_probe: float = 0.0


class Supervisor:
    """The launcher's state machine. `run()` returns the exit code."""

    def __init__(
        self,
        *,
        spawn: Spawn,
        ports: ApiPorts,
        events: EventSink,
        state: StateWriter,
        requests: StopRequests,
        cluster: Cluster | None,
        probe: HealthProbe = probe_health,
        previous: InstanceRecord | None = None,
        clock: Clock | None = None,
        timings: Timings | None = None,
    ) -> None:
        self._spawn_child = spawn
        self._ports = ports
        self._events = events
        self._state = state
        self._requests = requests
        self._cluster = cluster
        self._probe = probe
        self._previous = previous
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._timings = timings if timings is not None else Timings()
        self._api = _Supervised(API, RestartPolicy(self._timings))
        self._job_runner = _Supervised(JOB_RUNNER, RestartPolicy(self._timings))
        self._cluster_restarts = RestartPolicy(self._timings)
        self._cluster_started_at = 0.0
        self._cluster_restart_at: float | None = None
        self._arrival: ClusterArrival | None = None
        self._postmaster_pid: int | None = None
        self._ready = False
        self._announced_port: int | None = None
        self._last_ready_port = previous.last_ready_port if previous else None
        self.finished = threading.Event()
        """Set when `run()` has ended: what the end of the session waits for."""

    # ------------------------------------------------------------------
    # The whole life

    def run(self) -> int:
        try:
            return self._run()
        finally:
            self.finished.set()

    def _run(self) -> int:
        try:
            ready = self._start()
        except _StartRefusedError as refusal:
            logger.error("The launcher could not start: %s", refusal.line)
            self._events.emit(Event.FAILED, detail=refusal.line)
            self._stop(_Ending(refusal.code, StopReason.FAILURE), announce=False)
            return refusal.code
        except Exception as error:
            logger.exception("The launcher failed while starting")
            self._events.emit(
                Event.FAILED,
                detail=f"The launcher failed while starting ({error}); the "
                "traceback is in launcher.log.",
            )
            self._stop(_Ending(1, StopReason.FAILURE), announce=False)
            return 1
        if not ready:
            ending = _Ending(0, self._requested_reason())
        else:
            try:
                ending = self._supervise()
            except Exception as error:
                logger.exception("The supervision failed")
                ending = _Ending(
                    1,
                    StopReason.FAILURE,
                    f"The launcher failed ({error}); the traceback is in "
                    "launcher.log.",
                )
        self._stop(ending, announce=True)
        return ending.code

    # ------------------------------------------------------------------
    # The start (section 5.1)

    def _start(self) -> bool:
        """`True` once ready; `False` when a stop was asked first.

        **The API is created together with the cluster** -- the second branch
        of section 5.1, which section 16 made enter: warm, the cluster and the
        migrations took 2.1 s and the API 3.6 s from its creation to its first
        200, one after the other, 6.4 s to `ready` against a criterion of 5.
        Side by side, the import of the API covers the start of the cluster.
        What it costs, declared: the API answers 503 until the cluster is up,
        and could serve during the migrations -- to nobody who was told its
        URL, which `ready` alone gives. The job runner still waits for the
        migrations: its first query, the reaper's, would otherwise come before
        a first `initdb` has finished.

        **Except on the first run, when there is no cluster yet.** Measured
        too: the API's warm-up then imports `torch` and hashes 924 MB of
        models on the same disk as the `initdb`, which went from 45 to 99 s,
        and the cold start from 68 to 109 s. With no cluster to start, the
        API waits for the `initdb`, as in the first branch.
        """
        self._write_state(LauncherState.STARTING)
        if self._stop_asked():
            return False
        try:
            port = self._ports.first()
        except PortRefusedError as refusal:
            raise _StartRefusedError(str(refusal)) from None
        with_the_cluster = self._cluster is None or not self._first_run(self._cluster)
        if with_the_cluster:
            self._spawn_api(port, first=True)
        if self._cluster is None:
            self._arrival = ClusterArrival.EXTERNAL
        else:
            self._bring_up_cluster(self._cluster)
        self._write_state(LauncherState.STARTING)
        if self._stop_asked():
            return False
        if not with_the_cluster:
            self._spawn_api(port, first=True)
        self._spawn_job_runner(first=True)
        return self._wait_until_ready()

    @staticmethod
    def _first_run(cluster: Cluster) -> bool:
        """Whether `start()` is about to create the cluster: an `initdb` ahead."""
        try:
            return cluster.status().status is ClusterStatus.ABSENT
        except EmbeddedPostgresError:
            return False

    def _bring_up_cluster(self, cluster: Cluster) -> None:
        try:
            started = cluster.start(on_phase=self._phase)
            self._arrival = self._arrival_of(cluster, started)
            self._cluster_started_at = self._clock.monotonic()
            self._write_state(LauncherState.STARTING)
            if self._stop_asked():
                return
            prepared = cluster.prepare(started.connection, on_phase=self._phase)
        except EmbeddedPostgresError as error:
            raise _StartRefusedError(str(error)) from None
        for warning in prepared.warnings:
            logger.warning("%s", warning)

    def _arrival_of(self, cluster: Cluster, started: StartedCluster) -> ClusterArrival:
        """Section 7.3: a launcher stops the cluster a launcher started.

        Including the one a launcher that died left behind, which this one
        adopts and nobody else would stop: the state file of a launcher whose
        lock is free says `started`, about this same server.
        """
        try:
            self._postmaster_pid = cluster.status().pid
        except EmbeddedPostgresError as error:
            logger.warning("The cluster's status could not be read: %s", error)
            self._postmaster_pid = None
        if started.started_here:
            return ClusterArrival.STARTED
        previous = self._previous
        if (
            previous is not None
            and previous.cluster is ClusterArrival.STARTED
            and previous.postmaster_pid is not None
            and previous.postmaster_pid == self._postmaster_pid
        ):
            logger.info(
                "The cluster (PID %s) was started by a launcher that is gone; this "
                "one inherits it, and stops it at the end",
                self._postmaster_pid,
            )
            return ClusterArrival.STARTED
        return ClusterArrival.ADOPTED

    def _phase(self, phase: Phase) -> None:
        if self._ready:
            logger.info("Database: %s", phase.value)
        else:
            self._events.emit(Event.PHASE, phase=phase.value)

    def _wait_until_ready(self) -> bool:
        deadline = self._clock.monotonic() + self._timings.ready_deadline
        last_status: int | None = None
        while True:
            if self._stop_asked():
                return False
            fatal = self._watch_children()
            if fatal is not None:
                raise _StartRefusedError(fatal)
            self._restart_due()
            api = self._api
            if api.child is not None and api.port is not None:
                answer = self._probe(api.port)
                if answer is not None:
                    api.answering = True
                    last_status = answer.status
                    if answer.status == 200:
                        self._announce_ready(api.port, answer)
                        return True
            if self._clock.monotonic() >= deadline:
                raise _StartRefusedError(self._not_ready(last_status))
            self._clock.sleep(self._timings.tick)

    def _not_ready(self, last_status: int | None) -> str:
        seconds = f"{self._timings.ready_deadline:.0f} s"
        if last_status == 503:
            return (
                f"The API answered /health with 503 for {seconds}: it cannot reach "
                "the database."
            )
        if last_status is not None:
            return f"The API answered /health with {last_status} for {seconds}."
        detail = ""
        if self._api.child is not None:
            line = last_line(self._api.child.last_lines())
            if line:
                detail = f" Its last line: {line}"
        return f"The API did not answer on port {self._api.port} in {seconds}.{detail}"

    def _announce_ready(self, port: int, answer: HealthAnswer) -> None:
        self._ready = True
        self._announced_port = port
        self._last_ready_port = port
        self._write_state(LauncherState.READY)
        url = _url(port)
        logger.info("Ready at %s (search %s)", url, answer.search)
        self._events.emit(
            Event.READY, url=url, search=answer.search, indexing=self._indexing()
        )

    def _indexing(self) -> str:
        if self._job_runner.unavailable:
            return "unavailable"
        if self._job_runner.child is None:
            return "restarting"
        return "running"

    # ------------------------------------------------------------------
    # The supervision (section 6)

    def _supervise(self) -> _Ending:
        next_cluster_check = self._clock.monotonic() + self._timings.cluster_check
        while True:
            if self._stop_asked():
                return _Ending(0, self._requested_reason())
            fatal = self._watch_children()
            if fatal is not None:
                return _Ending(EXIT_CANNOT_START, StopReason.FAILURE, fatal)
            self._restart_due()
            self._probe_restarted_api()
            if (
                self._cluster is not None
                and self._clock.monotonic() >= next_cluster_check
            ):
                fatal = self._watch_cluster(self._cluster)
                if fatal is not None:
                    return _Ending(EXIT_CANNOT_START, StopReason.FAILURE, fatal)
                next_cluster_check = (
                    self._clock.monotonic() + self._timings.cluster_check
                )
            self._clock.sleep(self._timings.tick)

    def _watch_children(self) -> str | None:
        """Handle every child that exited; a line when the API is lost."""
        for supervised in (self._api, self._job_runner):
            child = supervised.child
            if child is None:
                continue
            code = child.poll()
            if code is None:
                continue
            supervised.child = None
            child.drain()
            fatal = self._exited(supervised, code, child.pid, child.last_lines())
            self._write_state(
                LauncherState.READY if self._ready else LauncherState.STARTING
            )
            if fatal is not None:
                return fatal
        return None

    def _exited(
        self, supervised: _Supervised, code: int, pid: int, lines: tuple[str, ...]
    ) -> str | None:
        name = supervised.program.name
        detail = last_line(lines)
        if code == EXIT_CANNOT_START:
            self._log_exit(name, code, pid, lines)
            return self._give_up(supervised, code, detail)
        if (
            supervised is self._api
            and code == UVICORN_STARTUP_FAILURE
            and not supervised.answering
        ):
            return self._port_taken(supervised)
        self._log_exit(name, code, pid, lines)
        now = self._clock.monotonic()
        delay = supervised.restarts.after_exit(now, supervised.started_at)
        if delay is None:
            minutes = self._timings.restart_window / 60
            return self._give_up(
                supervised,
                code,
                f"{name} exited {self._timings.restart_limit} times in "
                f"{minutes:.0f} minutes, and is not restarted again. Its last "
                f"line: {detail}",
            )
        supervised.restart_at = now + delay
        logger.warning("%s exited with %s; restarting in %.0f s", name, code, delay)
        self._events.emit(
            Event.CHILD,
            name=name,
            state="restarting",
            exit_code=code,
            delay=delay,
            detail=detail,
        )
        return None

    def _port_taken(self, api: _Supervised) -> str | None:
        """Section 5.4, step 3: between the probe and uvicorn's `bind`, the
        port went to another process. Another port, at once."""
        api.port_failures += 1
        taken = api.port
        if api.port_failures >= self._timings.port_attempts:
            return self._give_up(
                api,
                UVICORN_STARTUP_FAILURE,
                f"The API could not bind a port {api.port_failures} times in a row.",
            )
        try:
            port = self._ports.another(taken if taken is not None else 0)
        except PortRefusedError as refusal:
            return self._give_up(api, UVICORN_STARTUP_FAILURE, str(refusal))
        logger.warning(
            "Port %s was taken before the API bound it; trying %s", taken, port
        )
        self._spawn_api(port, port_retry=True)
        return None

    def _give_up(
        self, supervised: _Supervised, code: int, detail: str | None
    ) -> str | None:
        """No more restarts. Losing the API is losing the app."""
        supervised.unavailable = True
        supervised.restart_at = None
        name = supervised.program.name
        logger.error("%s is not restarted: %s", name, detail)
        self._events.emit(
            Event.CHILD, name=name, state="unavailable", exit_code=code, detail=detail
        )
        if supervised is self._api:
            return detail or f"The API exited with {code}."
        return None

    @staticmethod
    def _log_exit(name: str, code: int, pid: int, lines: tuple[str, ...]) -> None:
        """The launcher's log gets the last lines of whoever died, and only those."""
        logger.warning(
            "%s (PID %s) exited with %s; its last %d lines follow",
            name,
            pid,
            code,
            len(lines),
        )
        for line in lines:
            logger.warning("%s | %s", name, line)

    def _restart_due(self) -> None:
        now = self._clock.monotonic()
        for supervised in (self._api, self._job_runner):
            if (
                supervised.restart_at is None
                or supervised.child is not None
                or now < supervised.restart_at
            ):
                continue
            supervised.restart_at = None
            if supervised is self._api:
                self._spawn_api(self._ports.again(supervised.port))
            else:
                self._spawn_job_runner()

    def _probe_restarted_api(self) -> None:
        """A restarted API is followed until it answers, to say where it is."""
        api = self._api
        now = self._clock.monotonic()
        if (
            api.child is None
            or api.answering
            or api.port is None
            or now < api.next_probe
        ):
            return
        api.next_probe = now + self._timings.restart_probe
        answer = self._probe(api.port)
        if answer is None:
            return
        api.answering = True
        if answer.status == 200:
            self._last_ready_port = api.port
        if api.port != self._announced_port:
            self._announced_port = api.port
            logger.warning("The API came back on another port: %s", _url(api.port))
            self._events.emit(Event.ADDRESS, url=_url(api.port))
        self._events.emit(Event.CHILD, name=api.program.name, state="running")
        self._write_state(LauncherState.READY)

    def _watch_cluster(self, cluster: Cluster) -> str | None:
        """Section 6.2: only a dead postmaster is the supervisor's business.

        A server process that is not the postmaster dying makes the postmaster
        reinitialize on its own, with `ready` still in `postmaster.pid`;
        `status()` says `ready` all along, and nothing is done.
        """
        try:
            report = cluster.status()
        except EmbeddedPostgresError as error:
            logger.warning("The cluster's status could not be read: %s", error)
            return None
        if report.status in (
            ClusterStatus.READY,
            ClusterStatus.STARTING,
            ClusterStatus.STOPPING,
        ):
            return None
        if report.status is not ClusterStatus.STOPPED:
            found = report.status.value
            if report.detail:
                found = f"{found} ({report.detail})"
            return f"The database in {report.data_directory} is {found}."
        now = self._clock.monotonic()
        if self._cluster_restart_at is None:
            delay = self._cluster_restarts.after_exit(now, self._cluster_started_at)
            if delay is None:
                minutes = self._timings.restart_window / 60
                detail = (
                    f"The database stopped {self._timings.restart_limit} times in "
                    f"{minutes:.0f} minutes, and is not restarted again."
                )
                self._events.emit(
                    Event.CHILD, name=DATABASE, state="unavailable", detail=detail
                )
                return detail
            logger.warning("The database server is gone; starting it again")
            self._events.emit(
                Event.CHILD, name=DATABASE, state="restarting", delay=delay
            )
            self._cluster_restart_at = now + delay
        if now < self._cluster_restart_at:
            return None
        self._cluster_restart_at = None
        try:
            started = cluster.start(on_phase=self._phase)
        except (ClusterRefusedError, RuntimeInvalidError) as error:
            self._events.emit(
                Event.CHILD, name=DATABASE, state="unavailable", detail=str(error)
            )
            return str(error)
        except EmbeddedPostgresError as error:
            logger.warning("The database did not start again: %s", error)
            return None
        self._arrival = self._arrival_of(cluster, started)
        self._cluster_started_at = self._clock.monotonic()
        self._write_state(LauncherState.READY)
        self._events.emit(Event.CHILD, name=DATABASE, state="running")
        return None

    # ------------------------------------------------------------------
    # The children

    def _spawn_api(
        self, port: int, *, first: bool = False, port_retry: bool = False
    ) -> None:
        """The `phase` once, for the first API. A restarted one says `running`
        when it answers (`_probe_restarted_api`), or `ready` does."""
        api = self._api
        if not port_retry:
            api.port_failures = 0
        api.port = port
        api.answering = False
        api.next_probe = 0.0
        api.started_at = self._clock.monotonic()
        api.child = self._spawn_child(API, ["--port", str(port), SUPERVISED_FLAG])
        logger.info("Started the API (PID %s) on port %s", api.child.pid, port)
        if first:
            self._events.emit(Event.PHASE, phase=API.name)
        self._write_state(
            LauncherState.READY if self._ready else LauncherState.STARTING
        )

    def _spawn_job_runner(self, *, first: bool = False) -> None:
        """The `phase` for the first; `running` for every one that follows."""
        runner = self._job_runner
        runner.started_at = self._clock.monotonic()
        runner.child = self._spawn_child(JOB_RUNNER, [SUPERVISED_FLAG])
        logger.info("Started the job runner (PID %s)", runner.child.pid)
        if first:
            self._events.emit(Event.PHASE, phase=JOB_RUNNER.name)
        else:
            self._events.emit(Event.CHILD, name=JOB_RUNNER.name, state="running")
        self._write_state(
            LauncherState.READY if self._ready else LauncherState.STARTING
        )

    # ------------------------------------------------------------------
    # The stop (section 7)

    def _stop_asked(self) -> bool:
        return self._requests.count() > 0 or self._requests.session_ending()

    def _requested_reason(self) -> StopReason:
        if self._requests.session_ending():
            return StopReason.SESSION_END
        return StopReason.REQUESTED

    def _stop(self, ending: _Ending, *, announce: bool) -> None:
        if announce:
            fields: dict[str, object] = {"reason": ending.reason.value}
            if ending.detail:
                fields["detail"] = ending.detail
            self._events.emit(Event.STOPPING, **fields)
        self._write_state(LauncherState.STOPPING)
        self._stop_children()
        left_up = self._stop_cluster()
        self._state.write(
            InstanceRecord(
                state=LauncherState.STOPPED,
                cluster=ClusterArrival.STARTED if left_up else None,
                postmaster_pid=self._postmaster_pid if left_up else None,
                last_ready_port=self._last_ready_port,
            )
        )
        if announce:
            self._events.emit(Event.STOPPED, exit_code=ending.code)
        logger.info("Stopped (exit code %s)", ending.code)

    def _stop_children(self) -> None:
        """Both at once, each up to its deadline; `TerminateProcess` after it.

        Nothing depends on the other: the API and the job runner both need
        only the database, and so the database is stopped after them -- the
        `release()` of a job is a write.
        """
        stopping = [s for s in (self._api, self._job_runner) if s.child is not None]
        for supervised in stopping:
            supervised.restart_at = None
            assert supervised.child is not None
            supervised.child.request_stop()
        started_at = self._clock.monotonic()
        asked = self._requests.count()
        while stopping:
            now = self._clock.monotonic()
            skip = self._requests.count() > asked
            for supervised in list(stopping):
                child = supervised.child
                assert child is not None
                if child.poll() is not None:
                    child.drain()
                    supervised.child = None
                    stopping.remove(supervised)
                    logger.info(
                        "%s stopped in %.1f s",
                        supervised.program.name,
                        now - started_at,
                    )
                    continue
                if skip or now - started_at >= self._deadline(supervised):
                    self._terminate(supervised, child, skipped=skip)
                    supervised.child = None
                    stopping.remove(supervised)
            if stopping:
                self._clock.sleep(self._timings.tick)

    def _deadline(self, supervised: _Supervised) -> float:
        session = self._requests.session_ending()
        if supervised is self._api:
            return self._timings.session_api_stop if session else self._timings.api_stop
        return (
            self._timings.session_job_runner_stop
            if session
            else self._timings.job_runner_stop
        )

    def _terminate(
        self, supervised: _Supervised, child: Child, *, skipped: bool
    ) -> None:
        name = supervised.program.name
        why = (
            "a second request skipped the deadline"
            if skipped
            else f"it did not stop within {self._deadline(supervised):.0f} s"
        )
        # The launcher does not know whether the job runner held a job: if it
        # was still importing `torch` -- a DLL load does not return to Python
        # for seconds, and the interrupt waits for it -- it held none.
        cost = (
            "the requests in flight were cut"
            if supervised is self._api
            else "a job it held, if any, stays running until the reaper takes it "
            "back, up to 120 s after its last heartbeat, and costs an attempt"
        )
        logger.warning("Ended %s with TerminateProcess: %s; %s", name, why, cost)
        child.terminate()

    def _stop_cluster(self) -> bool:
        """Stop the cluster if a launcher started it; whether it was left up."""
        if self._cluster is None or self._arrival is not ClusterArrival.STARTED:
            return False
        started_at = time.monotonic()
        try:
            outcome = self._cluster.stop()
        except EmbeddedPostgresError as error:
            logger.error("The database could not be stopped: %s", error)
            return True
        logger.info(
            "Database: %s in %.1f s", outcome.value, time.monotonic() - started_at
        )
        return False

    # ------------------------------------------------------------------

    def _write_state(self, state: LauncherState) -> None:
        port = self._announced_port or self._api.port
        self._state.write(
            InstanceRecord(
                state=state,
                pid=os.getpid(),
                url=_url(port) if port is not None else None,
                children={
                    supervised.program.name: supervised.child.pid
                    for supervised in (self._api, self._job_runner)
                    if supervised.child is not None
                },
                cluster=self._arrival,
                postmaster_pid=self._postmaster_pid,
                last_ready_port=self._last_ready_port,
            )
        )


def _url(port: int) -> str:
    return f"http://{LOOPBACK}:{port}"
