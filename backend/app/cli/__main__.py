"""Unified command-line entry point over SolidVision's use cases.

    python -m app.cli index --root PATH [--label LABEL]
    python -m app.cli search "query text" [--limit N]
    python -m app.cli search-image PATH/TO/PICTURE.jpg [--limit N]

A thin dispatcher, not a new abstraction: each subcommand parses its own
arguments and calls straight into the composition an existing worker module
already owns -- `indexing_worker.run()` for indexing,
`app.cli.search.run()` for search, `app.cli.search.run_image()` for search
by picture. No business logic lives here.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="SolidVision command-line interface.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser(
        "index", help="Index every supported image under a folder."
    )
    index_parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help=(
            "Directory to scan. Required on purpose: there is no default, "
            "so this command can never start indexing a real photo "
            "collection nobody asked it to touch."
        ),
    )
    index_parser.add_argument(
        "--label",
        default="",
        help=(
            "User-facing name for the disk ROOT lives on -- 'HD2'. Used only "
            "the first time this volume is registered; an already-known "
            "device keeps its existing label."
        ),
    )

    search_parser = subparsers.add_parser(
        "search", help="Semantic search over the indexed images."
    )
    search_parser.add_argument(
        "query", help="Natural-language description of the image to find."
    )
    search_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of results (defaults to settings.top_k_results).",
    )

    image_parser = subparsers.add_parser(
        "search-image",
        help="Find the indexed images that look most like a given picture.",
    )
    image_parser.add_argument(
        "image",
        type=Path,
        help=(
            "The picture to search with -- e.g. a photo of a printed photo. "
            "Crop it to the picture first: the background is compared too."
        ),
    )
    image_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of results (defaults to settings.top_k_results).",
    )

    return parser


def main() -> None:
    args = _build_arg_parser().parse_args()

    if args.command == "index":
        from app.infrastructure.workers import indexing_worker

        indexing_worker.run(args.root.resolve(), args.label)
    elif args.command == "search":
        from app.cli import search

        search.run(args.query, args.limit)
    elif args.command == "search-image":
        from app.cli import search

        search.run_image(args.image, args.limit)


if __name__ == "__main__":
    main()
