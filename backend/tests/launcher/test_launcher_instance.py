"""One launcher per data root: the lock, the state file, the stop event (RFC-036 8).

Real files in a temporary directory, real `msvcrt` locks and real named
events: these are the operating system's behaviours the launcher rests on,
and a double would only test the double.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
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
    StopRequests,
    administrators_enabled,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="msvcrt locks and named events are Windows'"
)

RECORD = InstanceRecord(
    state=LauncherState.READY,
    pid=4120,
    url="http://127.0.0.1:51234",
    children={"api": 11, "job_runner": 12},
    cluster=ClusterArrival.STARTED,
    postmaster_pid=4242,
    last_ready_port=51234,
)


class TestTheRecord:
    def test_it_goes_to_json_and_back(self) -> None:
        assert InstanceRecord.from_json(RECORD.to_json()) == RECORD

    def test_a_record_of_another_format_is_refused(self) -> None:
        raw = RECORD.to_json() | {"format": 2}

        with pytest.raises(ValueError):
            InstanceRecord.from_json(raw)

    def test_a_record_with_a_wrong_type_is_refused(self) -> None:
        raw = RECORD.to_json() | {"last_ready_port": "51234"}

        with pytest.raises(ValueError):
            InstanceRecord.from_json(raw)


class TestTheStateFile:
    def test_what_is_written_is_read_back(self, tmp_path: Path) -> None:
        state = StateFile(tmp_path / "launcher.json")

        assert state.write(RECORD)

        assert state.read() == RECORD
        assert list(tmp_path.iterdir()) == [tmp_path / "launcher.json"]

    def test_no_file_is_no_record(self, tmp_path: Path) -> None:
        assert StateFile(tmp_path / "launcher.json").read() is None

    def test_a_file_that_is_not_json_is_no_record(self, tmp_path: Path) -> None:
        path = tmp_path / "launcher.json"
        path.write_text("{half a", encoding="utf-8")

        assert StateFile(path).read() is None

    def test_a_reader_never_sees_half_a_file(self, tmp_path: Path) -> None:
        """Rewritten in one rename (section 8): a `status` reading while the
        launcher writes sees the old record or the new one, never a mix."""
        state = StateFile(tmp_path / "launcher.json")
        state.write(RECORD)
        stop = threading.Event()
        written: list[int] = []

        def writer() -> None:
            port = 50000
            while not stop.is_set():
                port += 1
                state.write(
                    InstanceRecord(
                        state=LauncherState.READY,
                        url="http://127.0.0.1:" + str(port) + "/" + "x" * 20_000,
                        last_ready_port=port,
                    )
                )
                written.append(port)

        thread = threading.Thread(target=writer)
        thread.start()
        reads = 0
        deadline = time.monotonic() + 1.0
        try:
            while time.monotonic() < deadline:
                try:
                    text = (tmp_path / "launcher.json").read_text(encoding="utf-8")
                except PermissionError:
                    # Windows refuses an open at the instant of the rename;
                    # what must never happen is a file that does not parse.
                    continue
                record = InstanceRecord.from_json(json.loads(text))
                assert record.state is LauncherState.READY
                reads += 1
        finally:
            stop.set()
            thread.join(10)

        assert reads > 10
        assert len(written) > 10


class TestTheLock:
    def test_a_second_holder_is_refused_until_the_first_lets_go(
        self, tmp_path: Path
    ) -> None:
        first = InstanceLock(tmp_path / "launcher.lock")
        second = InstanceLock(tmp_path / "launcher.lock")

        assert first.acquire()
        try:
            assert not second.acquire(attempts=2, pause=0.01)
            assert second.held_elsewhere()
        finally:
            first.release()

        assert not second.held_elsewhere()
        assert second.acquire(attempts=1)
        second.release()

    def test_two_data_roots_hold_two_locks(self, tmp_path: Path) -> None:
        """A checkout and an installed app run side by side (section 8)."""
        checkout = InstanceLock(Instance(tmp_path / "SolidVision-dev").lock)
        installed = InstanceLock(Instance(tmp_path / "SolidVision").lock)

        assert checkout.acquire(attempts=1)
        try:
            assert installed.acquire(attempts=1)
            installed.release()
        finally:
            checkout.release()

    def test_the_lock_creates_the_data_root(self, tmp_path: Path) -> None:
        lock = InstanceLock(tmp_path / "new" / "launcher.lock")

        assert lock.acquire(attempts=1)
        lock.release()

        assert (tmp_path / "new" / "launcher.lock").is_file()


class TestTheStopEvent:
    def test_the_name_is_per_session_and_per_data_root(self, tmp_path: Path) -> None:
        checkout = Instance(tmp_path / "SolidVision-dev").stop_event_name
        installed = Instance(tmp_path / "SolidVision").stop_event_name

        assert checkout.startswith("Local\\SolidVision-launcher-")
        assert len(checkout) == len("Local\\SolidVision-launcher-") + 16
        assert checkout != installed
        assert Instance(tmp_path / "solidvision-DEV").stop_event_name == checkout

    def test_a_signal_is_seen_once(self) -> None:
        name = f"Local\\SolidVision-test-{uuid.uuid4().hex}"
        event = StopEvent.create(name)
        try:
            assert not event.is_set()
            assert StopEvent.signal(name)
            assert event.is_set()
            assert not event.is_set()
        finally:
            event.close()

    def test_signalling_a_launcher_that_is_not_there_says_so(self) -> None:
        assert not StopEvent.signal(f"Local\\SolidVision-test-{uuid.uuid4().hex}")


class TestStopRequests:
    def test_each_request_counts(self) -> None:
        requests = StopRequests()
        requests.ask()
        requests.ask()

        assert requests.count() == 2
        assert not requests.session_ending()

    def test_a_polled_source_counts_once_per_signal(self) -> None:
        arrived = [True, False, True]
        requests = StopRequests()
        requests.poll(lambda: arrived.pop(0) if arrived else False)

        assert [requests.count() for _ in range(4)] == [1, 1, 2, 2]

    def test_the_end_of_the_session_is_not_a_second_request(self) -> None:
        requests = StopRequests()
        requests.ask()
        requests.end_session()

        assert requests.count() == 1
        assert requests.session_ending()


def test_the_token_answers_whether_administrators_are_in_force() -> None:
    """Section 14: the group, not `TokenElevation`, asked of Windows -- which
    answers; `None` would mean it would not say."""
    assert isinstance(administrators_enabled(), bool)
