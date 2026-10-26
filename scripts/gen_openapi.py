"""Generate or verify the committed OpenAPI spec.

Without arguments, writes the spec to stdout. With ``--check``, compares
the freshly rendered spec against the committed ``docs/openapi.json`` and
exits non-zero on drift.

Used by:
    make openapi         -> write docs/openapi.json
    make openapi-check   -> CI drift gate
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(".env.testnet") # Use Testnet environment in openapi specs usage
# The Accounting service opens its store at import, and /data exists only in the container.
os.environ["ACCOUNTING_STATE_DIR"] = tempfile.mkdtemp()

from src.main import app

SPEC_PATH = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"


def render() -> str:
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    spec = render()
    if "--check" in argv:
        committed = SPEC_PATH.read_text(encoding="utf-8")
        if committed == spec:
            return 0
        sys.stderr.write(
            f"openapi.json drift detected at {SPEC_PATH.relative_to(Path.cwd())}.\n"
            "Run 'make openapi' to regenerate, then commit the result.\n"
        )
        return 1
    sys.stdout.write(spec)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
