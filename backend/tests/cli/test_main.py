"""Unit tests for the `app.cli` dispatcher: argument parsing and dispatch only.

No business logic lives in `app.cli.__main__`, so these tests never touch a
session or a model -- `indexing_worker.run()` and `search.run()` are
monkeypatched to record how the dispatcher called them, exactly as
`test_indexing_worker.py` monkeypatches the concrete classes `main()`
composes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.cli.__main__ import _build_arg_parser, main


class TestArgumentParsing:
    def test_a_command_is_required(self) -> None:
        with pytest.raises(SystemExit):
            _build_arg_parser().parse_args([])

    def test_index_requires_root(self) -> None:
        with pytest.raises(SystemExit):
            _build_arg_parser().parse_args(["index"])

    def test_index_accepts_root_and_label(self, tmp_path: Path) -> None:
        args = _build_arg_parser().parse_args(
            ["index", "--root", str(tmp_path), "--label", "HD2"]
        )

        assert args.command == "index"
        assert args.root == tmp_path
        assert args.label == "HD2"

    def test_index_label_defaults_to_empty(self, tmp_path: Path) -> None:
        args = _build_arg_parser().parse_args(["index", "--root", str(tmp_path)])

        assert args.label == ""

    def test_search_requires_a_query(self) -> None:
        with pytest.raises(SystemExit):
            _build_arg_parser().parse_args(["search"])

    def test_search_accepts_a_query_and_optional_limit(self) -> None:
        args = _build_arg_parser().parse_args(["search", "a lake", "--limit", "5"])

        assert args.command == "search"
        assert args.query == "a lake"
        assert args.limit == 5

    def test_search_limit_defaults_to_none(self) -> None:
        args = _build_arg_parser().parse_args(["search", "a lake"])

        assert args.limit is None


class TestDispatch:
    def test_index_is_dispatched_to_the_indexing_worker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[Path, str]] = []
        monkeypatch.setattr(
            "app.infrastructure.workers.indexing_worker.run",
            lambda root, label: calls.append((root, label)),
        )
        monkeypatch.setattr(
            "sys.argv",
            ["app.cli", "index", "--root", str(tmp_path), "--label", "HD2"],
        )

        main()

        assert calls == [(tmp_path.resolve(), "HD2")]

    def test_search_is_dispatched_to_the_search_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, int | None]] = []
        monkeypatch.setattr(
            "app.cli.search.run",
            lambda query, limit: calls.append((query, limit)),
        )
        monkeypatch.setattr(
            "sys.argv", ["app.cli", "search", "a lake", "--limit", "5"]
        )

        main()

        assert calls == [("a lake", 5)]
