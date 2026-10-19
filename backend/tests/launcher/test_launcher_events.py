"""The protocol on stdout: one JSON object per line (RFC-036 section 4.4)."""

from __future__ import annotations

import io
import json

from app.launcher.events import PROTOCOL, Event, JsonLines


def test_each_line_is_one_json_object_with_its_event() -> None:
    stream = io.StringIO()
    events = JsonLines(stream)

    events.emit(Event.STARTING, protocol=PROTOCOL, pid=1, mode="installed")
    events.emit(Event.READY, url="http://127.0.0.1:1", search="loading")

    lines = stream.getvalue().splitlines()
    assert [json.loads(line) for line in lines] == [
        {"event": "starting", "protocol": 1, "pid": 1, "mode": "installed"},
        {"event": "ready", "url": "http://127.0.0.1:1", "search": "loading"},
    ]


def test_every_line_is_ascii_whatever_it_carries() -> None:
    """So the reader's decoding of the pipe never matters."""
    stream = io.StringIO()

    JsonLines(stream).emit(Event.FAILED, detail="Łódź/東京/✈.jpg is not a folder")

    line = stream.getvalue()
    assert line.isascii()
    assert json.loads(line)["detail"] == "Łódź/東京/✈.jpg is not a folder"


def test_each_event_is_flushed_as_it_is_written() -> None:
    class Counting(io.StringIO):
        flushes = 0

        def flush(self) -> None:
            Counting.flushes += 1
            super().flush()

    events = JsonLines(Counting())
    events.emit(Event.STOPPING, reason="requested")
    events.emit(Event.STOPPED, exit_code=0)

    assert Counting.flushes == 2


def test_a_reader_that_went_away_does_not_stop_the_launcher() -> None:
    stream = io.StringIO()
    stream.close()

    JsonLines(stream).emit(Event.STOPPED, exit_code=0)


def test_the_protocol_is_version_1() -> None:
    assert PROTOCOL == 1
