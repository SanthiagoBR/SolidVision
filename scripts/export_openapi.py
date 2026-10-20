r"""Write the API's OpenAPI document into the frontend (RFC-037 section 4.3).

    python scripts/export_openapi.py            write frontend/src/api/openapi.json
    python scripts/export_openapi.py --check    exit 1 when the file is not current

The file is versioned, and each half of the drift check runs in the suite that
has its environment, with no server up (section 2.5): `pytest` compares what the
code produces with the file (`test_openapi_snapshot.py`), and `npm test`
compares `schema.d.ts` with what `openapi-typescript` generates from it. Whoever
changes the API without regenerating sees `pytest` fail; whoever regenerates
the document and not the types sees `npm test` fail. `npm run api:types` runs
this and then the generator: the one command of the frontend that needs Python.

**The defaults, and not the `.env` of whoever runs it** (section 11). The
document is `app.openapi()`, whose `info.title` and `info.version` come from
the `Settings`: written as they were loaded, a `PROJECT_VERSION` in somebody's
`.env` would make the snapshot theirs. They are written with the defaults of
the two fields. Nothing else in the document reads the configuration -- the
routes take their defaults from constants, and the search limit's default is
applied in the route, not declared.

Keys sorted, two spaces, UTF-8 and a final newline: a regenerated file differs
from the last one only where the API did.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DOCUMENT = REPO / "frontend" / "src" / "api" / "openapi.json"


def openapi_document() -> dict[str, Any]:
    """`app.openapi()`, with the title and the version of the defaults."""
    backend = str(REPO / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    from app.infrastructure.config.settings import Settings
    from app.presentation.api import app

    document = copy.deepcopy(app.openapi())
    document["info"]["title"] = Settings.model_fields["project_name"].default
    document["info"]["version"] = Settings.model_fields["project_version"].default
    # Through JSON, so that what is compared is what a file can hold.
    parsed: dict[str, Any] = json.loads(json.dumps(document))
    return parsed


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/export_openapi.py",
        description="Write the OpenAPI document the frontend's types come from.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Write nothing; exit 1 when the file differs from the code.",
    )
    arguments = parser.parse_args(argv)
    text = render(openapi_document())
    # `read_text` reads a CRLF as a LF: a checkout with `core.autocrlf` has the
    # file that way, and it is the same document.
    current = DOCUMENT.read_text(encoding="utf-8") if DOCUMENT.is_file() else None
    if arguments.check:
        if current != text:
            print(
                f"{DOCUMENT} is not what the code produces: run `npm run api:types` "
                "in frontend/.",
                file=sys.stderr,
            )
            return 1
        return 0
    if current == text:
        print(f"{DOCUMENT}: unchanged")
        return 0
    DOCUMENT.parent.mkdir(parents=True, exist_ok=True)
    DOCUMENT.write_text(text, encoding="utf-8", newline="\n")
    print(f"{DOCUMENT}: written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
