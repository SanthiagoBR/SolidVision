"""Ponto de entrada único de linha de comando sobre os casos de uso do SolidVision.

    python -m app.cli index --root PATH [--label LABEL]
    python -m app.cli search "query text" [--limit N] [--near LAT LON --radius M]
    python -m app.cli search-image PATH/TO/PICTURE.jpg [--limit N]
                                   [--near LAT LON --radius M]

Um despachante fino, e não uma nova abstração: cada subcomando lê os próprios
argumentos e chama direto a composição que um módulo de worker existente já
detém -- `indexing_worker.run()` para a indexação, `app.cli.search.run()`
para a busca, `app.cli.search.run_image()` para a busca por imagem. Nenhuma
regra de negócio mora aqui: `--near` e `--radius` são lidos como números e
repassados, e `app.cli.search` decide o que faz um círculo.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def _add_circle_arguments(parser: argparse.ArgumentParser) -> None:
    """`--near LAT LON --radius METRES`: só as fotos tiradas dentro do círculo.

    Os `near_lat`/`near_lon`/`radius_m` da API (RFC-032 §6). O centro é uma
    opção que recebe dois números, então latitude e longitude não podem ser
    dadas separadas; o raio é uma segunda opção sem padrão, porque completar um
    raio ausente inventaria justamente a grandeza que o deslocamento do drone
    torna crítica. O argparse lê `-26.3` aqui como número, e não como opção,
    já que nenhuma opção deste parser parece um número negativo.
    """
    parser.add_argument(
        "--near",
        nargs=2,
        type=float,
        metavar=("LAT", "LON"),
        default=None,
        help=(
            "Only photos taken within --radius of this point, in decimal "
            "degrees (e.g. --near -26.3214 -48.8163). A photo's position is "
            "where the drone was, not the ground it photographed."
        ),
    )
    parser.add_argument(
        "--radius",
        type=float,
        metavar="METRES",
        default=None,
        help=(
            "Radius of the --near circle, in metres. Required with --near and "
            "never defaulted; at least settings.min_radius_m (300 by default)."
        ),
    )


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
    _add_circle_arguments(search_parser)

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
    _add_circle_arguments(image_parser)

    return parser


def main() -> None:
    """Despacha, e transforma um modelo que não pode ser carregado numa linha.

    A linha nomeia o modelo, o arquivo e o que fazer (RFC-035 §6.5); um
    traceback só a enterraria. O código de saída é 78, que diz que rodar o
    comando de novo não vai ajudar (RFC-036 §6.1).
    """
    from app.infrastructure.ai.model_runtime import ModelUnavailableError
    from app.infrastructure.process.supervision import refuse_to_start

    args = _build_arg_parser().parse_args()

    try:
        _dispatch(args)
    except ModelUnavailableError as error:
        refuse_to_start(str(error))


def _dispatch(args: argparse.Namespace) -> None:
    if args.command == "index":
        from app.infrastructure.workers import indexing_worker

        indexing_worker.run(args.root.resolve(), args.label)
    elif args.command == "search":
        from app.cli import search

        search.run(args.query, args.limit, args.near, args.radius)
    elif args.command == "search-image":
        from app.cli import search

        search.run_image(args.image, args.limit, args.near, args.radius)


if __name__ == "__main__":
    main()
