"""The launcher as a process: the commands of RFC-036 section 4.3, for real.

Each test points `LOCALAPPDATA` at a temporary directory, so the data root is
the test's own, and plays the part of the other launcher by hand when it needs
one: it holds the lock, writes the state file, creates the stop event. No
cluster is started and no child is created.

The second opening is held to its promise -- it never reads the configuration
-- by giving it an invalid one: `BATCH_SIZE=auto` in the environment, which a
checkout reads. A launcher that imported the `Settings` would leave with 78.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.launcher.instance import (
    ClusterArrival,
    Instance,
    InstanceLock,
    InstanceRecord,
    LauncherState,
    StateFile,
    StopEvent,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows processes")

BACKEND = Path(__file__).resolve().parents[2]
CREATE_NO_WINDOW = 0x08000000
PROCESS_SECONDS = 120
INVALID = {"BATCH_SIZE": "auto"}


@pytest.fixture()
def local(tmp_path: Path) -> Path:
    directory = tmp_path / "Local"
    directory.mkdir()
    return directory


def instance_of(local: Path) -> Instance:
    """A checkout's data root under `local` (RFC-034 section 6.2)."""
    return Instance(local / "SolidVision-dev")


def launcher(
    local: Path, *arguments: str, extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app.launcher", *arguments],
        env={
            **os.environ,
            "PYTHONPATH": str(BACKEND),
            "LOCALAPPDATA": str(local),
            **(extra or {}),
        },
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=PROCESS_SECONDS,
        creationflags=CREATE_NO_WINDOW,
    )


def events_of(completed: subprocess.CompletedProcess[str]) -> list[dict[str, object]]:
    events = [json.loads(line) for line in completed.stdout.splitlines()]
    assert all("event" in event for event in events)
    return events


def test_a_second_launcher_says_where_the_first_is_and_leaves(local: Path) -> None:
    instance = instance_of(local)
    lock = InstanceLock(instance.lock)
    assert lock.acquire()
    try:
        StateFile(instance.state).write(
            InstanceRecord(
                state=LauncherState.READY, pid=4120, url="http://127.0.0.1:51234"
            )
        )

        completed = launcher(local, extra=INVALID)
    finally:
        lock.release()

    assert completed.returncode == 0, completed.stderr[-2000:]
    assert events_of(completed) == [
        {
            "event": "already_running",
            "protocol": 1,
            "pid": 4120,
            "url": "http://127.0.0.1:51234",
            "state": "ready",
        }
    ]
    assert "cannot start" not in completed.stderr


def test_an_invalid_configuration_is_starting_then_failed_and_78(local: Path) -> None:
    """The launcher refuses for its children, which would read the same file
    and refuse the same way (section 5.1); the first line carries `protocol`."""
    completed = launcher(local, extra=INVALID)

    assert completed.returncode == 78
    starting, failed = events_of(completed)
    assert starting["event"] == "starting"
    assert starting["protocol"] == 1
    assert starting["mode"] == "development"
    assert failed["event"] == "failed"
    assert str(failed["detail"]).startswith(
        "SolidVision cannot start: environment variable: BATCH_SIZE"
    )
    assert "Traceback" not in completed.stderr


def test_a_refused_start_keeps_what_the_last_launcher_left(local: Path) -> None:
    """The launcher claims the data root in the state file before it reads the
    configuration -- so that a second opening sees `starting` and its PID --
    and keeps what the last launcher left about the cluster and the port, which
    the next one still needs (sections 5.4 and 7.3)."""
    instance = instance_of(local)
    StateFile(instance.state).write(
        InstanceRecord(
            state=LauncherState.READY,
            pid=7,
            cluster=ClusterArrival.STARTED,
            postmaster_pid=4242,
            last_ready_port=51234,
        )
    )

    completed = launcher(local, extra=INVALID)

    assert completed.returncode == 78
    assert StateFile(instance.state).read() == InstanceRecord(
        state=LauncherState.STOPPED,
        cluster=ClusterArrival.STARTED,
        postmaster_pid=4242,
        last_ready_port=51234,
    )


def test_status_without_a_launcher(local: Path) -> None:
    completed = launcher(local, "status")

    assert completed.returncode == 0
    assert "launcher:  not running" in completed.stdout


def test_status_with_a_launcher_up(local: Path) -> None:
    instance = instance_of(local)
    lock = InstanceLock(instance.lock)
    assert lock.acquire()
    try:
        StateFile(instance.state).write(
            InstanceRecord(
                state=LauncherState.READY,
                pid=4120,
                url="http://127.0.0.1:51234",
                children={"api": 11, "job_runner": 12},
                cluster=ClusterArrival.STARTED,
            )
        )

        completed = launcher(local, "status")
    finally:
        lock.release()

    assert completed.returncode == 0
    assert "launcher:  running, ready (PID 4120)" in completed.stdout
    assert "url:       http://127.0.0.1:51234" in completed.stdout
    assert "cluster:   started" in completed.stdout


def test_stop_without_a_launcher_or_a_cluster_says_so(local: Path) -> None:
    completed = launcher(local, "stop")

    assert completed.returncode == 0
    assert "no launcher is running" in completed.stderr


def test_stop_without_a_launcher_leaves_an_adopted_cluster_alone(local: Path) -> None:
    """Only a cluster a launcher started is stopped by `stop` (section 4.3)."""
    instance = instance_of(local)
    StateFile(instance.state).write(
        InstanceRecord(state=LauncherState.READY, cluster=ClusterArrival.ADOPTED)
    )

    completed = launcher(local, "stop", extra=INVALID)

    assert completed.returncode == 0
    assert "no launcher is running" in completed.stderr


def test_stop_asks_the_launcher_and_waits_for_its_lock(local: Path) -> None:
    instance = instance_of(local)
    lock = InstanceLock(instance.lock)
    assert lock.acquire()
    event = StopEvent.create(instance.stop_event_name)
    stopping = subprocess.Popen(
        [sys.executable, "-m", "app.launcher", "stop"],
        env={**os.environ, "PYTHONPATH": str(BACKEND), "LOCALAPPDATA": str(local)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
        text=True,
    )
    try:
        deadline = time.monotonic() + PROCESS_SECONDS
        while not event.is_set():
            assert time.monotonic() < deadline, "stop never signalled the event"
            assert stopping.poll() is None, stopping.communicate()
            time.sleep(0.05)
        time.sleep(0.5)
        assert stopping.poll() is None, "stop did not wait for the lock"
    finally:
        event.close()
        lock.release()
    _, error = stopping.communicate(timeout=PROCESS_SECONDS)

    assert stopping.returncode == 0
    assert "launcher: stopped" in error
