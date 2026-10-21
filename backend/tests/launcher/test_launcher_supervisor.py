"""The supervisor's state machine, against doubles and a fake clock (RFC-036 10).

No process is created, no cluster started, and no time passes but the fake
clock's, which moves only when the supervisor sleeps. What is asserted is the
order of things and when they happen on that clock: the start of section 5.1,
the supervision of section 6, the stop of section 7, and who stops the cluster.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tests.launcher.fakes import Behavior, FakePorts, Harness, log_lines

from app.infrastructure.database.embedded.cluster import ClusterStatus
from app.infrastructure.database.embedded.errors import (
    ClusterRefusedError,
    StartupFailedError,
)
from app.launcher.__main__ import SESSION_STOP_SECONDS
from app.launcher.events import Event
from app.launcher.instance import ClusterArrival, InstanceRecord, LauncherState
from app.launcher.supervisor import (
    ApiPorts,
    PortRefusedError,
    RestartPolicy,
    Timings,
    exit_code,
)

TICK = 0.1
CONSOLE_CTRL_C = 0xC000013A
CANNOT_START = 78
SKEW = 2 * TICK + 0.01
"""How far a moment of the fake clock may sit from the one computed by hand:
the supervisor looks at the world once per tick."""


def near(expected: float) -> object:
    return pytest.approx(expected, abs=SKEW)


@pytest.fixture()
def launcher_log() -> Iterator[list[str]]:
    with log_lines("app.app.launcher.supervisor") as lines:
        yield lines


# --------------------------------------------------------------------------
# The start (section 5.1)
# --------------------------------------------------------------------------


class TestTheStart:
    def test_the_api_starts_with_the_cluster_and_the_job_runner_after_it(
        self,
    ) -> None:
        """The second branch of section 5.1, which section 16 made enter: the
        API's import covers the cluster's start; the job runner, whose first
        query is the reaper's, waits for the migrations."""
        harness = Harness()
        harness.stop_at(5.0)

        assert harness.run() == 0

        phases = [fields["phase"] for fields in harness.events.of(Event.PHASE)]
        assert phases == ["api", "starting", "migrating", "job_runner"]
        assert harness.cluster is not None
        assert [call for _, call in harness.cluster.calls] == [
            "start",
            "prepare",
            "stop",
        ]
        assert [child.created_at for child in harness.spawner.children] == [0.0, 0.0]

    def test_on_a_first_run_the_api_waits_for_the_initdb(self) -> None:
        """The refinement of the second branch, measured: an `initdb` that
        shares the disk with the API's warm-up took 99 s instead of 45."""
        harness = Harness(cluster_absent=True)
        harness.stop_at(5.0)

        assert harness.run() == 0

        phases = [fields["phase"] for fields in harness.events.of(Event.PHASE)]
        assert phases == ["initdb", "starting", "migrating", "api", "job_runner"]

    def test_the_two_children_are_started_together_with_their_arguments(
        self,
    ) -> None:
        harness = Harness()
        harness.stop_at(5.0)

        harness.run()

        (api,) = harness.api()
        (runner,) = harness.job_runner()
        assert api.arguments == ["--port", "61000", "--supervised"]
        assert runner.arguments == ["--supervised"]

    def test_ready_is_the_first_200_of_health(self) -> None:
        """A 503 is "not yet": with the cluster stopped, /health answers 503
        in 6 ms (RFC-034 section 16), and that is no reason to give up."""
        harness = Harness()
        harness.probe.status = 503

        def the_database_answers() -> None:
            harness.probe.status = 200

        harness.clock.at(5.0, the_database_answers)
        harness.stop_at(10.0)

        harness.run()

        assert harness.events.when(Event.READY) == [near(5.0)]
        assert harness.events.of(Event.READY) == [
            {
                "url": "http://127.0.0.1:61000",
                "search": "on_demand",
                "indexing": "running",
            }
        ]

    def test_health_is_asked_every_tick_while_starting(self) -> None:
        harness = Harness()
        harness.probe.after = 1.0
        harness.stop_at(5.0)

        harness.run()

        before_ready = [moment for moment, _ in harness.probe.asked if moment <= 1.0]
        assert len(before_ready) == pytest.approx(10, abs=1)

    def test_ready_carries_what_health_said_of_search(self) -> None:
        harness = Harness()
        harness.probe.search = "loading"
        harness.stop_at(5.0)

        harness.run()

        assert harness.events.of(Event.READY)[0]["search"] == "loading"

    def test_without_an_answer_in_time_the_start_fails(self) -> None:
        harness = Harness()
        harness.probe.status = 503

        assert harness.run() == CANNOT_START

        (failed,) = harness.events.of(Event.FAILED)
        assert "503" in str(failed["detail"])
        assert harness.events.when(Event.FAILED) == [near(Timings().ready_deadline)]
        assert Event.READY.value not in harness.events.names()

    def test_a_failed_start_stops_the_cluster_it_started(self) -> None:
        harness = Harness()
        harness.probe.status = 503

        harness.run()

        assert harness.cluster is not None
        assert harness.cluster.asked("stop") != []
        assert all(child.poll() is not None for child in harness.spawner.children)

    def test_a_refused_cluster_is_failed_and_78(self) -> None:
        harness = Harness()
        assert harness.cluster is not None
        harness.cluster.start_failures.append(
            ClusterRefusedError("the password-cluster pair is lost")
        )

        assert harness.run() == CANNOT_START

        assert harness.events.of(Event.FAILED) == [
            {"detail": "the password-cluster pair is lost"}
        ]
        (api,) = harness.spawner.children
        assert api.name == "api"
        assert api.poll() is not None

    def test_a_stop_asked_while_the_cluster_starts_waits_for_it(self) -> None:
        """Nothing is cut in half (section 7.1): the request is honoured when
        `start()` returns, and the cluster this launcher started is stopped."""
        harness = Harness()
        assert harness.cluster is not None
        harness.cluster.during_start = harness.requests.ask

        assert harness.run() == 0

        assert [child.name for child in harness.spawner.children] == ["api"]
        assert harness.api()[0].stop_requested_at is not None
        assert [call for _, call in harness.cluster.calls] == ["start", "stop"]
        assert harness.events.names()[-2:] == ["stopping", "stopped"]

    def test_with_the_external_source_no_database_is_touched(self) -> None:
        harness = Harness(with_cluster=False)
        harness.stop_at(5.0)

        assert harness.run() == 0

        assert harness.events.when(Event.READY) == [near(1.0)]
        assert all(
            record.cluster in (ClusterArrival.EXTERNAL, None)
            for record in harness.states.written
        )
        phases = [fields["phase"] for fields in harness.events.of(Event.PHASE)]
        assert phases == ["api", "job_runner"]

    def test_an_unexpected_error_while_starting_is_failed_and_1(self) -> None:
        """Not a refusal: 1, the code of "anything else", with the traceback in
        the launcher's log -- and the cluster it started is stopped all the same."""
        harness = Harness()
        assert harness.cluster is not None

        def the_interpreter_goes() -> None:
            harness.spawner.failure = OSError("python.exe is gone")

        # After the API and during the cluster's start: the job runner fails.
        harness.cluster.during_start = the_interpreter_goes

        assert harness.run() == 1

        (failed,) = harness.events.of(Event.FAILED)
        assert "python.exe is gone" in str(failed["detail"])
        assert harness.cluster is not None
        assert harness.cluster.asked("stop") != []


# --------------------------------------------------------------------------
# The port (section 5.4)
# --------------------------------------------------------------------------


class TestThePort:
    def test_the_last_ready_port_is_taken_again(self) -> None:
        assert ApiPorts(FakePorts(), fixed=None, last=51234).first() == 51234

    def test_a_busy_last_port_gives_way_to_one_the_system_gives(self) -> None:
        assert (
            ApiPorts(FakePorts(busy=[51234]), fixed=None, last=51234).first() == 61000
        )

    def test_api_port_is_used_whatever_the_last_port_was(self) -> None:
        assert ApiPorts(FakePorts(), fixed=8000, last=51234).first() == 8000

    def test_a_busy_api_port_is_refused_with_a_line(self) -> None:
        with pytest.raises(PortRefusedError, match="API_PORT is 8000"):
            ApiPorts(FakePorts(busy=[8000]), fixed=8000, last=None).first()

    def test_a_restart_tries_the_same_port_first(self) -> None:
        ports = ApiPorts(FakePorts(), fixed=None, last=None)

        assert ports.again(51234) == 51234
        assert (
            ApiPorts(FakePorts(busy=[51234]), fixed=None, last=None).again(51234)
            == 61000
        )

    def test_a_busy_api_port_fails_the_start_with_78(self) -> None:
        harness = Harness(fixed_port=8000, busy=[8000])

        assert harness.run() == CANNOT_START

        (failed,) = harness.events.of(Event.FAILED)
        assert str(failed["detail"]).startswith("API_PORT is 8000")
        assert harness.spawner.children == []

    def test_an_api_that_lost_its_port_before_binding_gets_another(self) -> None:
        """Exit 3 before the API answered: uvicorn's `STARTUP_FAILURE`, the port
        taken between the probe and the bind. Not a death: no wait, no count."""
        harness = Harness()
        harness.spawner.script(
            "api", Behavior(exits_after=0.2, exit_code=3), Behavior()
        )
        harness.stop_at(10.0)

        assert harness.run() == 0

        assert [child.port for child in harness.api()] == [61000, 61001]
        assert harness.events.of(Event.READY)[0]["url"] == "http://127.0.0.1:61001"
        assert harness.events.of(Event.CHILD) == []

    def test_three_ports_lost_in_a_row_are_the_end(self) -> None:
        harness = Harness()
        harness.spawner.script("api", Behavior(exits_after=0.2, exit_code=3))

        assert harness.run() == CANNOT_START

        assert len(harness.api()) == 3
        (failed,) = harness.events.of(Event.FAILED)
        assert "3 times" in str(failed["detail"])


# --------------------------------------------------------------------------
# The supervision of the children (section 6.1)
# --------------------------------------------------------------------------


class TestRestarts:
    def test_78_from_the_job_runner_is_not_restarted(self) -> None:
        line = "SolidVision cannot start: The models are not in C:\\nowhere."
        harness = Harness()
        harness.spawner.script(
            "job_runner", Behavior(exits_after=0.5, exit_code=78, lines=("", line))
        )
        harness.stop_at(60.0)

        assert harness.run() == 0

        assert len(harness.job_runner()) == 1
        assert harness.events.of(Event.CHILD) == [
            {
                "name": "job_runner",
                "state": "unavailable",
                "exit_code": 78,
                "detail": line,
            }
        ]
        assert harness.events.of(Event.READY)[0]["indexing"] == "unavailable"

    def test_78_from_the_api_before_ready_fails_the_start(self) -> None:
        line = "SolidVision cannot start: solidvision.env: BATCH_SIZE: not a number"
        harness = Harness()
        harness.spawner.script(
            "api", Behavior(exits_after=0.5, exit_code=78, lines=(line,))
        )

        assert harness.run() == CANNOT_START

        assert harness.events.of(Event.FAILED) == [{"detail": line}]
        assert len(harness.api()) == 1

    def test_losing_the_api_after_ready_stops_everything_with_78(self) -> None:
        harness = Harness()
        harness.spawner.script(
            "api", Behavior(exits_after=5.0, exit_code=78, lines=("the line",))
        )

        assert harness.run() == CANNOT_START

        assert harness.events.names()[-4:] == ["ready", "child", "stopping", "stopped"]
        assert harness.events.of(Event.STOPPING) == [
            {"reason": "failure", "detail": "the line"}
        ]
        assert harness.events.of(Event.STOPPED) == [{"exit_code": CANNOT_START}]
        (runner,) = harness.job_runner()
        assert runner.stop_requested_at == near(5.0)

    def test_the_wait_grows_and_five_exits_are_the_end(self) -> None:
        """0, 2, 5 and 15 s, and the fifth exit in ten minutes is unavailable."""
        harness = Harness()
        harness.spawner.script("job_runner", Behavior(exits_after=1.0, exit_code=1))
        harness.stop_at(300.0)

        assert harness.run() == 0

        created = [child.created_at for child in harness.job_runner()]
        assert created == [near(0.0), near(1.0), near(4.0), near(10.0), near(26.0)]
        children = harness.events.of(Event.CHILD)
        assert [fields["state"] for fields in children] == [
            "restarting",
            "running",
            "restarting",
            "running",
            "restarting",
            "running",
            "restarting",
            "running",
            "unavailable",
        ]
        assert [
            fields["delay"] for fields in children if fields["state"] == "restarting"
        ] == [0.0, 2.0, 5.0, 15.0]
        assert children[-1]["state"] == "unavailable"
        assert "5 times in 10 minutes" in str(children[-1]["detail"])

    def test_a_restarted_api_comes_back_on_its_port(self) -> None:
        harness = Harness()
        harness.spawner.script(
            "api", Behavior(exits_after=5.0, exit_code=1), Behavior()
        )
        harness.stop_at(20.0)

        assert harness.run() == 0

        assert [child.port for child in harness.api()] == [61000, 61000]
        assert [fields["state"] for fields in harness.events.of(Event.CHILD)] == [
            "restarting",
            "running",
        ]
        assert harness.events.of(Event.ADDRESS) == []

    def test_a_restarted_api_on_another_port_says_where(self) -> None:
        harness = Harness()
        harness.spawner.script(
            "api", Behavior(exits_after=5.0, exit_code=1), Behavior()
        )
        harness.clock.at(4.0, lambda: harness.ports.busy.add(61000))
        harness.stop_at(20.0)

        harness.run()

        assert [child.port for child in harness.api()] == [61000, 61001]
        assert harness.events.of(Event.ADDRESS) == [{"url": "http://127.0.0.1:61001"}]
        assert harness.states.last.last_ready_port == 61001

    def test_the_policy_waits_longer_each_time_and_forgets_after_ten_minutes(
        self,
    ) -> None:
        policy = RestartPolicy(Timings())

        assert policy.after_exit(10.0, 0.0) == 0.0
        assert policy.after_exit(20.0, 10.0) == 2.0
        assert policy.after_exit(30.0, 22.0) == 5.0
        assert policy.after_exit(735.0, 35.0) == 0.0

    def test_the_longest_wait_is_thirty_seconds(self) -> None:
        """Exits spread out, so that ten minutes never hold five of them, and
        never up long enough to start the count again."""
        policy = RestartPolicy(Timings())

        delays = [
            policy.after_exit(moment, moment - 190.0)
            for moment in range(200, 1400, 200)
        ]

        assert delays == [0.0, 2.0, 5.0, 15.0, 30.0, 30.0]

    def test_five_exits_in_ten_minutes_are_the_end(self) -> None:
        policy = RestartPolicy(Timings())

        delays = [policy.after_exit(moment, moment - 1.0) for moment in (1, 2, 3, 4, 5)]

        assert delays == [0.0, 2.0, 5.0, 15.0, None]


# --------------------------------------------------------------------------
# The supervision of the cluster (section 6.2)
# --------------------------------------------------------------------------


class TestTheCluster:
    def test_a_dead_postmaster_is_started_again_within_two_seconds(self) -> None:
        harness = Harness()
        assert harness.cluster is not None
        harness.clock.at(10.0, harness.cluster.lose_the_postmaster)
        harness.stop_at(30.0)

        assert harness.run() == 0

        starts = harness.cluster.asked("start")
        assert len(starts) == 2
        assert 10.0 <= starts[1] <= 10.0 + Timings().cluster_check + SKEW
        assert len(harness.api()) == 1
        assert len(harness.job_runner()) == 1
        assert [
            (fields["name"], fields["state"])
            for fields in harness.events.of(Event.CHILD)
        ] == [("database", "restarting"), ("database", "running")]

    def test_the_cluster_restarted_by_the_launcher_is_its_to_stop(self) -> None:
        harness = Harness(cluster_running=True)
        assert harness.cluster is not None
        harness.clock.at(10.0, harness.cluster.lose_the_postmaster)
        harness.stop_at(30.0)

        harness.run()

        assert harness.cluster.asked("stop") != []

    def test_a_cluster_that_keeps_failing_to_start_is_the_end(self) -> None:
        harness = Harness()
        assert harness.cluster is not None
        cluster = harness.cluster

        def lose_it_for_good() -> None:
            cluster.lose_the_postmaster()
            cluster.start_failures.extend(StartupFailedError("no") for _ in range(10))

        harness.clock.at(10.0, lose_it_for_good)

        assert harness.run() == CANNOT_START

        (stopping,) = harness.events.of(Event.STOPPING)
        assert stopping["reason"] == "failure"
        assert "5 times in 10 minutes" in str(stopping["detail"])

    def test_a_refusal_on_the_restart_is_the_end_at_once(self) -> None:
        harness = Harness()
        assert harness.cluster is not None
        cluster = harness.cluster

        def refuse() -> None:
            cluster.lose_the_postmaster()
            cluster.start_failures.append(ClusterRefusedError("the pair is lost"))

        harness.clock.at(10.0, refuse)

        assert harness.run() == CANNOT_START

        assert harness.events.of(Event.STOPPING)[0]["detail"] == "the pair is lost"

    def test_a_cluster_that_changed_under_the_launcher_is_the_end(self) -> None:
        harness = Harness()
        assert harness.cluster is not None
        cluster = harness.cluster
        harness.clock.at(
            10.0,
            lambda: setattr(cluster, "status_override", ClusterStatus.OTHER_RUNTIME),
        )

        assert harness.run() == CANNOT_START

        assert "other runtime" in str(harness.events.of(Event.STOPPING)[0]["detail"])

    def test_a_healthy_cluster_is_only_looked_at(self) -> None:
        """A server process that is not the postmaster dying is the postmaster's
        to handle: `status()` keeps saying ready, and nothing is done."""
        harness = Harness()
        harness.stop_at(21.0)

        harness.run()

        assert harness.cluster is not None
        assert harness.cluster.asked("start") == [0.0]
        assert harness.cluster.status_calls >= 10


# --------------------------------------------------------------------------
# The stop (section 7)
# --------------------------------------------------------------------------


class TestTheStop:
    def test_both_children_first_and_the_cluster_last(self) -> None:
        harness = Harness()
        harness.stop_at(5.0)

        harness.run()

        (api,) = harness.api()
        (runner,) = harness.job_runner()
        assert api.stop_requested_at == near(5.0)
        assert runner.stop_requested_at == near(5.0)
        assert harness.cluster is not None
        (stopped_at,) = harness.cluster.asked("stop")
        assert api.exited_at is not None and runner.exited_at is not None
        assert stopped_at >= max(api.exited_at, runner.exited_at)
        assert api.terminated_at is None and runner.terminated_at is None
        assert harness.events.names()[-2:] == ["stopping", "stopped"]
        assert harness.events.of(Event.STOPPING) == [{"reason": "requested"}]
        assert harness.events.of(Event.STOPPED) == [{"exit_code": 0}]

    def test_a_child_past_its_deadline_is_terminated(self) -> None:
        harness = Harness()
        harness.spawner.script("job_runner", Behavior(stops_after=None))
        harness.stop_at(5.0)

        assert harness.run() == 0

        (runner,) = harness.job_runner()
        (api,) = harness.api()
        assert runner.terminated_at == near(5.0 + Timings().job_runner_stop)
        assert api.terminated_at is None

    def test_the_api_has_its_own_deadline(self) -> None:
        harness = Harness()
        harness.spawner.script("api", Behavior(stops_after=None))
        harness.stop_at(5.0)

        harness.run()

        (api,) = harness.api()
        assert api.terminated_at == near(5.0 + Timings().api_stop)

    def test_a_second_request_skips_the_deadlines(self) -> None:
        harness = Harness()
        harness.spawner.script("job_runner", Behavior(stops_after=None))
        harness.stop_at(5.0)
        harness.stop_at(7.0)

        harness.run()

        (runner,) = harness.job_runner()
        assert runner.terminated_at == near(7.0)

    def test_the_end_of_the_session_shortens_the_deadlines(self) -> None:
        harness = Harness()
        harness.spawner.script("job_runner", Behavior(stops_after=20.0))
        harness.clock.at(5.0, harness.requests.end_session)

        assert harness.run() == 0

        (runner,) = harness.job_runner()
        assert runner.terminated_at == near(5.0 + Timings().session_job_runner_stop)
        assert harness.events.of(Event.STOPPING) == [{"reason": "session_end"}]

    def test_a_child_dead_before_the_stop_is_said_to_be(
        self, launcher_log: list[str]
    ) -> None:
        """A logoff that ends the job runner first: within the tick in which
        the stop is asked, before the supervisor looks at the children."""
        harness = Harness()

        def logoff() -> None:
            harness.job_runner()[-1].die(CONSOLE_CTRL_C)
            harness.requests.end_session()

        harness.clock.at(5.0, logoff)

        assert harness.run() == 0

        (runner,) = harness.job_runner()
        assert runner.stop_requested_at is None
        assert runner.terminated_at is None
        (line,) = [entry for entry in launcher_log if entry.startswith("job_runner")]
        assert "exited with 3221225786 (0xC000013A) before the stop asked" in line
        assert "api stopped in 0.3 s, with 0" in launcher_log

    def test_a_child_that_stops_says_with_what(self, launcher_log: list[str]) -> None:
        """A `release()` that failed leaves with 1 and a traceback on stderr,
        which the log of the launcher does not hold."""
        harness = Harness()
        harness.spawner.script("job_runner", Behavior(stops_after=0.5))
        harness.stop_at(5.0)

        harness.run()

        assert "job_runner stopped in 0.5 s, with 0" in launcher_log

    def test_an_exit_code_is_written_in_hex_when_it_is_an_ntstatus(self) -> None:
        assert exit_code(0) == "0"
        assert exit_code(78) == "78"
        assert exit_code(CONSOLE_CTRL_C) == "3221225786 (0xC000013A)"
        assert exit_code(-1) == "-1 (0xFFFFFFFF)"

    def test_a_child_waiting_to_restart_is_not_started_by_the_stop(self) -> None:
        harness = Harness()
        harness.spawner.script(
            "job_runner", Behavior(exits_after=1.0), Behavior(exits_after=1.0)
        )
        harness.stop_at(2.5)

        harness.run()

        assert len(harness.job_runner()) == 2


# --------------------------------------------------------------------------
# Who stops the cluster (section 7.3)
# --------------------------------------------------------------------------


class TestWhoStopsTheCluster:
    def test_a_cluster_this_launcher_started_is_stopped(self) -> None:
        harness = Harness()
        harness.stop_at(5.0)

        harness.run()

        assert harness.cluster is not None
        assert len(harness.cluster.asked("stop")) == 1
        assert any(
            record.cluster is ClusterArrival.STARTED
            for record in harness.states.written
        )

    def test_an_adopted_cluster_is_left_up(self) -> None:
        harness = Harness(cluster_running=True)
        harness.stop_at(5.0)

        harness.run()

        assert harness.cluster is not None
        assert harness.cluster.asked("stop") == []
        assert harness.cluster.running
        assert any(
            record.cluster is ClusterArrival.ADOPTED
            for record in harness.states.written
        )

    def test_an_adopted_cluster_is_stopped_at_the_end_of_the_session(self) -> None:
        """Windows would end it a moment later; in order, the next start needs
        no crash recovery (section 7.5)."""
        harness = Harness(cluster_running=True)
        harness.clock.at(5.0, harness.requests.end_session)

        harness.run()

        assert harness.cluster is not None
        (stopped_at,) = harness.cluster.asked("stop_by_signal")
        (runner,) = harness.job_runner()
        assert runner.exited_at is not None and stopped_at >= runner.exited_at
        assert not harness.cluster.running
        assert harness.states.last.cluster is None

    def test_at_the_end_of_the_session_the_cluster_is_stopped_by_its_signal(
        self,
    ) -> None:
        """No `pg_ctl`: a process started during a logoff dies at birth."""
        harness = Harness()
        harness.clock.at(5.0, harness.requests.end_session)

        harness.run()

        assert harness.cluster is not None
        assert harness.cluster.asked("stop") == []
        assert len(harness.cluster.asked("stop_by_signal")) == 1
        assert harness.cluster.signal_timeouts == [Timings().session_cluster_stop]

    def test_an_ordinary_stop_still_uses_pg_ctl(self) -> None:
        harness = Harness()
        harness.stop_at(5.0)

        harness.run()

        assert harness.cluster is not None
        assert harness.cluster.asked("stop_by_signal") == []
        assert len(harness.cluster.asked("stop")) == 1

    def test_the_session_stop_fits_in_the_windows_wait(self) -> None:
        """The window waits 30 s for the whole stop (section 7.5): the job
        runner's deadline, then up to three waits of the cluster's."""
        timings = Timings()
        worst = timings.session_job_runner_stop + 3 * timings.session_cluster_stop

        assert worst < SESSION_STOP_SECONDS

    def test_the_cluster_of_a_launcher_that_died_is_inherited(self) -> None:
        dead = InstanceRecord(
            state=LauncherState.READY,
            pid=7,
            cluster=ClusterArrival.STARTED,
            postmaster_pid=4242,
        )
        harness = Harness(cluster_running=True, previous=dead)
        harness.stop_at(5.0)

        harness.run()

        assert harness.cluster is not None
        assert len(harness.cluster.asked("stop")) == 1

    def test_a_server_started_by_hand_since_is_not_inherited(self) -> None:
        """The dead launcher's server is not this one: somebody ran `embedded
        start` in between, and that cluster is theirs."""
        dead = InstanceRecord(
            state=LauncherState.READY,
            pid=7,
            cluster=ClusterArrival.STARTED,
            postmaster_pid=1111,
        )
        harness = Harness(cluster_running=True, previous=dead)
        harness.stop_at(5.0)

        harness.run()

        assert harness.cluster is not None
        assert harness.cluster.asked("stop") == []

    def test_an_ordered_stop_leaves_no_ownership_behind(self) -> None:
        """So that a later launcher that adopts a cluster somebody started by
        hand does not stop it on the word of this one."""
        harness = Harness()
        harness.stop_at(5.0)

        harness.run()

        final = harness.states.last
        assert final.state is LauncherState.STOPPED
        assert final.cluster is None
        assert final.pid is None
        assert final.last_ready_port == 61000


# --------------------------------------------------------------------------
# The state file (section 8)
# --------------------------------------------------------------------------


class TestTheStateFile:
    def test_it_follows_the_life_of_the_launcher(self) -> None:
        harness = Harness()
        harness.stop_at(5.0)

        harness.run()

        states = [record.state for record in harness.states.written]
        assert states[0] is LauncherState.STARTING
        assert LauncherState.READY in states
        assert states[-2:] == [LauncherState.STOPPING, LauncherState.STOPPED]
        ready = next(
            r for r in harness.states.written if r.state is LauncherState.READY
        )
        assert ready.url == "http://127.0.0.1:61000"
        assert set(ready.children) == {"api", "job_runner"}
        assert ready.last_ready_port == 61000

    def test_the_last_port_survives_an_ordered_stop(self) -> None:
        harness = Harness(last_port=51234)
        harness.stop_at(5.0)

        harness.run()

        assert harness.api()[0].port == 51234
        assert harness.states.last.last_ready_port == 51234
