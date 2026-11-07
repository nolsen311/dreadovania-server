"""Write the OpenAPI spec to docs/openapi.json, or check that it is up to date.

The committed spec is the contract with dreadovania-nx (ADR 0007), so CI fails if
the code and the committed file disagree.

Usage:
  python scripts/export_openapi.py          # regenerate docs/openapi.json
  python scripts/export_openapi.py --check  # exit 1 if it is stale
"""

import json
import sys
from pathlib import Path

from dreadovania.app import create_app

SPEC_PATH = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"


def render() -> str:
    spec = create_app().openapi()
    # The package version changes every release; keep it out of the contract diff.
    spec["info"].pop("version", None)
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    rendered = render()
    if "--check" in argv:
        current = SPEC_PATH.read_text() if SPEC_PATH.exists() else ""
        if current != rendered:
            print(
                f"{SPEC_PATH.name} is out of date. Run: uv run python scripts/export_openapi.py",
                file=sys.stderr,
            )
            return 1
        print(f"{SPEC_PATH.name} is up to date")
        return 0
    SPEC_PATH.write_text(rendered)
    print(f"wrote {SPEC_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
