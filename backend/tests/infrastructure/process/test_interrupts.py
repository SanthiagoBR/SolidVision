"""Interrupts that wait for the database call in flight (RFC-036 section 7.5).

The signal is real -- `signal.raise_signal(SIGINT)`, what the stdin watch
sends -- and so is the handler; the "database call" is any block a test puts
inside `deferred()`. What only a logoff shows, that no new server process is
needed afterwards, is for the hand check of section 16.
"""

from __future__ import annotations

import signal
from collections.abc import Iterator

import pytest

from app.infrastructure.process.interrupts import deferred, deferring, install


@pytest.fixture(autouse=True)
def handler() -> Iterator[None]:
    previous = signal.getsignal(signal.SIGINT)
    install()
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def interrupt() -> None:
    signal.raise_signal(signal.SIGINT)


def test_outside_a_deferred_block_the_interrupt_is_raised_at_once() -> None:
    done: list[str] = []

    with pytest.raises(KeyboardInterrupt):
        interrupt()
        done.append("after")

    assert done == []


def test_inside_one_it_waits_for_the_block_to_end() -> None:
    done: list[str] = []

    with pytest.raises(KeyboardInterrupt):
        with deferred():
            interrupt()
            done.append("the rest of the call")
        done.append("after the block")

    assert done == ["the rest of the call"]


def test_nested_blocks_raise_at_the_outermost_end() -> None:
    done: list[str] = []

    with pytest.raises(KeyboardInterrupt):
        with deferred():
            with deferred():
                interrupt()
            done.append("between the two ends")

    assert done == ["between the two ends"]


def test_a_block_without_an_interrupt_raises_nothing() -> None:
    with deferred():
        pass
    with deferred():
        pass


def test_the_interrupt_is_raised_once() -> None:
    with pytest.raises(KeyboardInterrupt):
        with deferred():
            interrupt()

    with deferred():
        pass


def test_an_error_leaving_the_block_gives_way_to_the_interrupt() -> None:
    """What the operator asked for wins; the error stays as its context."""
    with pytest.raises(KeyboardInterrupt) as raised:
        with deferred():
            interrupt()
            raise ValueError("the call failed")

    assert isinstance(raised.value.__context__, ValueError)


class Repository:
    def __init__(self) -> None:
        self.saved: list[int] = []
        self.label = "jobs"

    def save(self, value: int) -> int:
        interrupt()  # the query is in flight when the stop comes
        self.saved.append(value)
        return value

    def count(self) -> int:
        return len(self.saved)


def test_every_method_of_a_deferring_repository_finishes_before_the_interrupt() -> None:
    repository = Repository()
    wrapped = deferring(repository)

    with pytest.raises(KeyboardInterrupt):
        wrapped.save(7)

    assert repository.saved == [7]


def test_a_deferring_repository_is_otherwise_the_repository() -> None:
    repository = Repository()
    repository.saved.append(1)
    wrapped = deferring(repository)

    assert wrapped.count() == 1
    assert wrapped.label == "jobs"
    assert wrapped.save.__name__ == "save"
