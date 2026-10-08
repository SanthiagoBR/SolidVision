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

    def test_search_image_requires_a_picture(self) -> None:
        with pytest.raises(SystemExit):
            _build_arg_parser().parse_args(["search-image"])

    def test_search_image_accepts_a_path_and_optional_limit(
        self, tmp_path: Path
    ) -> None:
        picture = tmp_path / "print.jpg"

        args = _build_arg_parser().parse_args(
            ["search-image", str(picture), "--limit", "20"]
        )

        assert args.command == "search-image"
        assert args.image == picture
        assert args.limit == 20

    def test_search_image_limit_defaults_to_none(self, tmp_path: Path) -> None:
        args = _build_arg_parser().parse_args(["search-image", str(tmp_path / "a.jpg")])

        assert args.limit is None

    def test_search_accepts_a_circle_with_negative_coordinates(self) -> None:
        """`-26.3` is read as a number, not as an unknown option."""
        args = _build_arg_parser().parse_args(
            ["search", "a roof", "--near", "-26.3214", "-48.8163", "--radius", "300"]
        )

        assert args.near == [-26.3214, -48.8163]
        assert args.radius == 300.0

    def test_search_image_accepts_a_circle(self, tmp_path: Path) -> None:
        args = _build_arg_parser().parse_args(
            [
                "search-image",
                str(tmp_path / "a.jpg"),
                "--near",
                "-26.3214",
                "-48.8163",
                "--radius",
                "500",
            ]
        )

        assert args.near == [-26.3214, -48.8163]
        assert args.radius == 500.0

    def test_the_circle_defaults_to_none(self) -> None:
        args = _build_arg_parser().parse_args(["search", "a roof"])

        assert args.near is None
        assert args.radius is None

    def test_near_needs_both_coordinates(self) -> None:
        with pytest.raises(SystemExit):
            _build_arg_parser().parse_args(
                ["search", "a roof", "--near", "-26.3214", "--radius", "300"]
            )


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
        calls: list[tuple[object, ...]] = []
        monkeypatch.setattr(
            "app.cli.search.run",
            lambda query, limit, near, radius_m: calls.append(
                (query, limit, near, radius_m)
            ),
        )
        monkeypatch.setattr(
            "sys.argv",
            [
                "app.cli",
                "search",
                "a lake",
                "--limit",
                "5",
                "--near",
                "-26.3214",
                "-48.8163",
                "--radius",
                "300",
            ],
        )

        main()

        assert calls == [("a lake", 5, [-26.3214, -48.8163], 300.0)]

    def test_search_image_is_dispatched_to_the_search_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[object, ...]] = []
        monkeypatch.setattr(
            "app.cli.search.run_image",
            lambda image, limit, near, radius_m: calls.append(
                (image, limit, near, radius_m)
            ),
        )
        picture = tmp_path / "print.jpg"
        monkeypatch.setattr(
            "sys.argv", ["app.cli", "search-image", str(picture), "--limit", "20"]
        )

        main()

        assert calls == [(picture, 20, None, None)]

    def test_half_a_circle_stops_before_anything_is_opened(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real `search.run()`, with a model and a session that must not load."""

        def refuse() -> None:
            raise AssertionError("nothing may be opened for half a circle")

        monkeypatch.setattr("app.presentation.dependencies.get_embedding_model", refuse)
        monkeypatch.setattr(
            "app.infrastructure.persistence.session.SessionLocal", refuse
        )
        monkeypatch.setattr(
            "sys.argv", ["app.cli", "search", "a roof", "--radius", "300"]
        )

        with pytest.raises(SystemExit, match="go together"):
            main()
