"""Doubles for the supervisor (RFC-036 section 10): no process, no cluster, no time.

The clock moves only when the supervisor sleeps, so a test of five restarts in
ten minutes runs in milliseconds, and an action can be scheduled at a moment
of that clock -- "ask for the stop at 30 s". A child is a script: when it
exits by itself, with what code, and how long it takes to stop once its stdin
is closed. The probe answers for whichever API child holds the port, alive and
old enough. The cluster records what it was asked, and when. Every event is
recorded with the moment it was emitted.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

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
from app.launcher.children import API, Program
from app.launcher.events import Event
from app.launcher.instance import InstanceRecord, StopRequests
from app.launcher.supervisor import ApiPorts, HealthAnswer, Supervisor, Timings

LIMIT_SECONDS = 100_000.0
"""A supervisor that has not ended by this moment of the fake clock never will."""

EPSILON = 1e-9


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@contextmanager
def log_lines(name: str) -> Iterator[list[str]]:
    """What one logger of the launcher says, from INFO up.

    A handler of its own on that logger, and not `caplog`: the `app` logger
    stops propagation once it is configured, and before that the same record
    would reach the capture twice."""
    logger = logging.getLogger(name)
    handler = _Lines()
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield handler.lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self._scheduled: list[tuple[float, Callable[[], None]]] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.now > LIMIT_SECONDS:
            raise AssertionError("the supervisor never ended")
        due = [entry for entry in self._scheduled if entry[0] <= self.now + EPSILON]
        for entry in due:
            self._scheduled.remove(entry)
            entry[1]()

    def at(self, moment: float, action: Callable[[], None]) -> None:
        self._scheduled.append((moment, action))


@dataclass(frozen=True)
class Behavior:
    """What one child does."""

    exits_after: float | None = None
    """Exits by itself this long after it was created; `None`: never."""
    exit_code: int = 1
    stops_after: float | None = 0.3
    """Exits with 0 this long after its stdin was closed; `None`: never."""
    lines: tuple[str, ...] = ()


_PIDS = itertools.count(1000)


class FakeChild:
    def __init__(
        self,
        program: Program,
        arguments: Sequence[str],
        clock: FakeClock,
        behavior: Behavior,
    ) -> None:
        self.program = program
        self.arguments = list(arguments)
        self.behavior = behavior
        self.created_at = clock.now
        self.stop_requested_at: float | None = None
        self.terminated_at: float | None = None
        self.exited_at: float | None = None
        self._clock = clock
        self._pid = next(_PIDS)
        self._code: int | None = None

    @property
    def name(self) -> str:
        return self.program.name

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def port(self) -> int | None:
        if "--port" in self.arguments:
            return int(self.arguments[self.arguments.index("--port") + 1])
        return None

    def poll(self) -> int | None:
        if self._code is not None:
            return self._code
        now = self._clock.now
        behavior = self.behavior
        if (
            behavior.exits_after is not None
            and now - self.created_at >= behavior.exits_after - EPSILON
        ):
            self._exit(behavior.exit_code)
        elif (
            self.stop_requested_at is not None
            and behavior.stops_after is not None
            and now - self.stop_requested_at >= behavior.stops_after - EPSILON
        ):
            self._exit(0)
        return self._code

    def request_stop(self) -> None:
        if self.stop_requested_at is None:
            self.stop_requested_at = self._clock.now

    def terminate(self) -> None:
        self.terminated_at = self._clock.now
        self._exit(1)

    def die(self, code: int) -> None:
        """Ended from outside, by nobody the launcher knows: a logoff, say."""
        self._exit(code)

    def drain(self, timeout: float = 2.0) -> bool:
        return True

    def last_lines(self) -> tuple[str, ...]:
        return self.behavior.lines

    def _exit(self, code: int) -> None:
        if self._code is None:
            self._code = code
            self.exited_at = self._clock.now


class Spawner:
    """`Spawn`: each program's children follow a script, the last one repeating."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self._scripts: dict[str, list[Behavior]] = {}
        self.children: list[FakeChild] = []
        self.failure: Exception | None = None
        """Raised instead of creating a child: the interpreter is gone, say."""

    def script(self, name: str, *behaviors: Behavior) -> None:
        self._scripts[name] = list(behaviors)

    def __call__(self, program: Program, arguments: Sequence[str]) -> FakeChild:
        if self.failure is not None:
            raise self.failure
        script = self._scripts.get(program.name, [])
        behavior = script.pop(0) if len(script) > 1 else (script or [Behavior()])[0]
        child = FakeChild(program, arguments, self._clock, behavior)
        self.children.append(child)
        return child

    def of(self, name: str) -> list[FakeChild]:
        return [child for child in self.children if child.name == name]


class Probe:
    """`/health` of whichever API child holds the port, alive and old enough."""

    def __init__(
        self,
        spawner: Spawner,
        clock: FakeClock,
        *,
        after: float = 1.0,
        status: int = 200,
        search: str = "on_demand",
    ) -> None:
        self._spawner = spawner
        self._clock = clock
        self.after = after
        self.status = status
        self.search = search
        self.asked: list[tuple[float, int]] = []

    def __call__(self, port: int) -> HealthAnswer | None:
        self.asked.append((self._clock.now, port))
        for child in reversed(self._spawner.of(API.name)):
            if (
                child.port == port
                and child.poll() is None
                and self._clock.now - child.created_at >= self.after - EPSILON
            ):
                return HealthAnswer(self.status, self.search)
        return None


class FakePorts:
    """`Prober`: busy ports refuse, and the system hands out 61000, 61001, ..."""

    def __init__(self, busy: Sequence[int] = ()) -> None:
        self.busy = set(busy)
        self._free = itertools.count(61000)

    def usable(self, port: int) -> bool:
        return port not in self.busy

    def free_port(self) -> int:
        while True:
            port = next(self._free)
            if port not in self.busy:
                return port


class FakeCluster:
    """`Cluster`: records what it was asked, and when; can lose its postmaster.

    Running or not when the launcher arrives: `start()` on a stopped one is
    `started_here`, on a running one an adoption -- the adapter's rule.
    """

    def __init__(
        self, clock: FakeClock, *, running: bool = False, absent: bool = False
    ) -> None:
        self._clock = clock
        self.running = running
        self.absent = absent
        """No cluster yet: the next `start()` creates it."""
        self.pid = 4242
        self.calls: list[tuple[float, str]] = []
        self.signal_timeouts: list[float] = []
        self.status_calls = 0
        self.status_override: ClusterStatus | None = None
        self.start_failures: list[Exception] = []
        self.during_start: Callable[[], None] | None = None
        """Something that happens while `start()` runs: a Ctrl+C, say."""

    def start(self, on_phase: Callable[[Phase], None] | None = None) -> StartedCluster:
        self.calls.append((self._clock.now, "start"))
        if self.during_start is not None:
            self.during_start()
        if self.start_failures:
            raise self.start_failures.pop(0)
        if on_phase is not None:
            if self.absent:
                on_phase(Phase.INITDB)
            on_phase(Phase.STARTING)
        self.absent = False
        here = not self.running
        self.running = True
        return StartedCluster(
            connection=ClusterConnection(port=55432, password="secret"),
            started_here=here,
            created=False,
            recovered=False,
        )

    def prepare(
        self,
        connection: ClusterConnection,
        on_phase: Callable[[Phase], None] | None = None,
    ) -> PreparedDatabase:
        self.calls.append((self._clock.now, "prepare"))
        if on_phase is not None:
            on_phase(Phase.MIGRATING)
        return PreparedDatabase(
            created_database=False, migrated_from=None, heads=("head",), warnings=()
        )

    def status(self) -> ClusterReport:
        self.status_calls += 1
        if self.absent:
            return ClusterReport(ClusterStatus.ABSENT, Path("C:/data"))
        if self.status_override is not None:
            return ClusterReport(self.status_override, Path("C:/data"), detail="odd")
        if self.running:
            return ClusterReport(
                ClusterStatus.READY, Path("C:/data"), port=55432, pid=self.pid
            )
        return ClusterReport(ClusterStatus.STOPPED, Path("C:/data"))

    def stop(self) -> StopOutcome:
        self.calls.append((self._clock.now, "stop"))
        was_running = self.running
        self.running = False
        return StopOutcome.FAST if was_running else StopOutcome.NOT_RUNNING

    def stop_by_signal(self, timeout: float) -> StopOutcome:
        self.calls.append((self._clock.now, "stop_by_signal"))
        self.signal_timeouts.append(timeout)
        was_running = self.running
        self.running = False
        return StopOutcome.FAST if was_running else StopOutcome.NOT_RUNNING

    def lose_the_postmaster(self) -> None:
        """The postmaster is killed: the next server will have another PID."""
        self.running = False
        self.pid += 1

    def asked(self, call: str) -> list[float]:
        return [moment for moment, name in self.calls if name == call]


class Events:
    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.emitted: list[tuple[float, Event, dict[str, object]]] = []

    def emit(self, event: Event, **fields: object) -> None:
        self.emitted.append((self._clock.now, event, fields))

    def names(self) -> list[str]:
        return [event.value for _, event, _ in self.emitted]

    def of(self, event: Event) -> list[dict[str, object]]:
        return [fields for _, kind, fields in self.emitted if kind is event]

    def when(self, event: Event) -> list[float]:
        return [moment for moment, kind, _ in self.emitted if kind is event]


class States:
    def __init__(self) -> None:
        self.written: list[InstanceRecord] = []

    def write(self, record: InstanceRecord) -> bool:
        self.written.append(record)
        return True

    @property
    def last(self) -> InstanceRecord:
        return self.written[-1]


@dataclass
class Harness:
    """One supervisor and everything around it, with the defaults of a start
    that works: a cluster this launcher starts, an API that answers 200 one
    second after it is created, a job runner that runs."""

    fixed_port: int | None = None
    last_port: int | None = None
    busy: Sequence[int] = ()
    previous: InstanceRecord | None = None
    with_cluster: bool = True
    cluster_running: bool = False
    cluster_absent: bool = False
    timings: Timings = field(default_factory=Timings)

    def __post_init__(self) -> None:
        self.clock = FakeClock()
        self.requests = StopRequests()
        self.events = Events(self.clock)
        self.states = States()
        self.spawner = Spawner(self.clock)
        self.probe = Probe(self.spawner, self.clock)
        self.ports = FakePorts(self.busy)
        self.cluster = (
            FakeCluster(
                self.clock, running=self.cluster_running, absent=self.cluster_absent
            )
            if self.with_cluster
            else None
        )

    def supervisor(self) -> Supervisor:
        return Supervisor(
            spawn=self.spawner,
            ports=ApiPorts(self.ports, fixed=self.fixed_port, last=self.last_port),
            events=self.events,
            state=self.states,
            requests=self.requests,
            cluster=self.cluster,
            probe=self.probe,
            previous=self.previous,
            clock=self.clock,
            timings=self.timings,
        )

    def stop_at(self, moment: float) -> None:
        self.clock.at(moment, self.requests.ask)

    def run(self) -> int:
        return self.supervisor().run()

    def api(self) -> list[FakeChild]:
        return self.spawner.of("api")

    def job_runner(self) -> list[FakeChild]:
        return self.spawner.of("job_runner")
