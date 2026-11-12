"""Export the API contract the frontend's TypeScript types are generated from.

    python -m api.contracts           # write contracts/*.json
    python -m api.contracts --check   # exit 1 if they are stale (CI)

- contracts/openapi.json: REST endpoints and models (FastAPI's OpenAPI).
- contracts/agent-events.schema.json: the SSE event union (`api.schemas.AgentEvent`).
  It isn't in the OpenAPI document because the run endpoints return the stream as a
  response object (D-041).

The frontend runs `npm run gen:types` on these files; CI checks both steps for drift.
Output is deterministic (sorted keys, fixed settings), so a diff means a real change.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from api.main import create_app
from api.schemas import AGENT_EVENT
from core.config import Settings

OUT_DIR = Path("contracts")


def documents() -> dict[str, dict[str, Any]]:
    settings = Settings(_env_file=None, app_env="test")
    openapi = create_app(settings, runtime_factory=None).openapi()
    events = AGENT_EVENT.json_schema(mode="serialization")
    events["title"] = "AgentEvent"
    return {"openapi.json": openapi, "agent-events.schema.json": events}


def render(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m api.contracts")
    parser.add_argument("--check", action="store_true", help="fail if the files are stale")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)
    stale = []
    for name, doc in documents().items():
        path, text = args.out / name, render(doc)
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == text:
            continue
        if args.check:
            stale.append(str(path))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            print(f"wrote {path}")
    if stale:
        print(
            "Stale API contracts: " + ", ".join(stale) + "\n"
            "Run `make contracts` (or .\\make.ps1 contracts) and commit the result."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
