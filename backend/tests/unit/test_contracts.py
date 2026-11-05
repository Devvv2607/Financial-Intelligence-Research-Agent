"""The committed API contracts match the backend schemas (the frontend types come from them)."""

import json
from pathlib import Path

from api.contracts import OUT_DIR, documents, main, render

REPO = Path(__file__).resolve().parents[3]


def test_committed_contracts_are_current() -> None:
    for name, doc in documents().items():
        committed = (REPO / OUT_DIR / name).read_text(encoding="utf-8")
        assert committed == render(doc), f"{name} is stale: run `make contracts`"


def test_event_schema_covers_every_event_type() -> None:
    schema = documents()["agent-events.schema.json"]
    kinds = set(schema["discriminator"]["mapping"])
    assert {"run_started", "node", "token", "final", "error", "clarification_needed"} <= kinds
    assert len(kinds) == len(schema["oneOf"])


def test_check_fails_on_a_stale_file(tmp_path: Path) -> None:
    assert main(["--out", str(tmp_path)]) == 0  # writes
    assert main(["--check", "--out", str(tmp_path)]) == 0
    stale = tmp_path / "openapi.json"
    doc = json.loads(stale.read_text(encoding="utf-8"))
    doc["info"]["version"] = "0.0.0"
    stale.write_text(render(doc), encoding="utf-8")
    assert main(["--check", "--out", str(tmp_path)]) == 1
