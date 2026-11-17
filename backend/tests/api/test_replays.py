"""Recorded runs: the recorder (against the scripted runtime), the fixtures, playback."""

import itertools
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from api import scripted
from api.main import create_app
from api.replay_recorder import find_leaks, main, record, render
from api.replays import RecordedEvent, Replay, load_replays, playback_delay_ms
from api.schemas import AGENT_EVENT
from core.config import Settings

from .test_api_agent import events, kinds

E2E_REPLAYS = Path(__file__).resolve().parents[3] / "frontend" / "e2e" / "fixtures" / "replays"


def app_settings(replays_dir: Path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        agent_runtime="scripted",
        groq_api_key=SecretStr("gsk_unused"),
        api_runs_per_minute=100,
        live_runs_per_visitor_per_day=100,
        live_runs_per_ip_per_day=100,
        replays_dir=replays_dir,
        replay_delay_factor=0,
    )


@pytest.fixture
async def client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(scripted, "TOOL_DELAY_S", 0.0)
    monkeypatch.setattr(scripted, "TOKEN_DELAY_S", 0.0)
    app = create_app(app_settings(E2E_REPLAYS))
    # The recorder asks /api/runs for token counts; without the local stack that route
    # fails, which must read as "unknown", not crash the test.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    headers = {"X-Visitor-Id": str(uuid.uuid4())}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c,
    ):
        yield c


async def test_recorder_captures_both_segments_and_labels_the_runtime(
    client: httpx.AsyncClient,
) -> None:
    replay = await record(client, "bank", "How risky is the bank?", "JPMorgan Chase (JPM)")
    assert replay.source == "scripted"  # from X-Agent-Runtime, not from a flag
    assert [s.input.kind for s in replay.segments] == ["run", "resume"]
    first, second = (s.events for s in replay.segments)
    assert any(e.event.event == "clarification_needed" for e in first)
    assert second[0].event.event == "run_started" and second[-1].event.event in {"final", "node"}
    assert all(b.t_ms >= a.t_ms for a, b in itertools.pairwise(first))
    Replay.model_validate_json(render(replay))  # the file round-trips


async def test_recorder_needs_an_answer_when_the_agent_asks(client: httpx.AsyncClient) -> None:
    with pytest.raises(RuntimeError, match="pass --answer"):
        await record(client, "bank", "How risky is the bank?")


def test_render_refuses_keys_and_ip_addresses() -> None:
    assert find_leaks("filed 12 Nov 2026, item 5.02, version 1.40.3") == []
    assert find_leaks("from 203.0.113.7 via proxy")[0].startswith("IPv4")
    assert find_leaks("key gsk_abcdefghijklmnop")[0].startswith("Groq API key")
    assert find_leaks("Authorization: Bearer abc.def.ghijklmnop")[0].startswith("bearer")
    replay = Replay.model_validate_json((E2E_REPLAYS / "tesla-advice.json").read_text("utf-8"))
    leaky = replay.model_copy(update={"api_url": "http://10.0.0.5:8000"})
    with pytest.raises(ValueError, match="IPv4"):
        render(leaky)


def test_cli_spends_nothing_without_yes(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--api", "https://api.example.com", "--slug", "x", "--question", "q"]) == 2
    assert "--yes" in capsys.readouterr().out


def test_committed_replays_are_valid_and_honestly_labeled() -> None:
    for folder, source in [(E2E_REPLAYS, "scripted"), (Path("replays"), "live")]:
        files = sorted(folder.glob("*.json"))
        loaded = load_replays(folder)
        assert sorted(loaded) == [f.stem for f in files], f"invalid replay in {folder}"
        for replay in loaded.values():
            assert replay.source == source  # test fixtures never pose as live runs
            assert find_leaks(render(replay)) == []


async def test_replays_are_listed_and_streamed_per_segment(client: httpx.AsyncClient) -> None:
    listed = {r["slug"]: r for r in (await client.get("/api/replays")).json()}
    assert set(listed) == {"bank-clarification", "tesla-advice"}
    bank = listed["bank-clarification"]
    assert [i["kind"] for i in bank["inputs"]] == ["run", "resume"] and bank["source"] == "scripted"

    no_visitor = {"X-Visitor-Id": ""}  # replays need no visitor id and no runtime
    first = events(await client.get("/api/replays/bank-clarification", headers=no_visitor))
    assert "clarification_needed" in kinds(first)
    second = events(await client.get("/api/replays/bank-clarification?segment=1"))
    assert kinds(second)[0] == "run_started" and "final" in kinds(second)
    assert (await client.get("/api/replays/bank-clarification?segment=2")).status_code == 404
    assert (await client.get("/api/replays/nope")).status_code == 404


def test_broken_or_misnamed_fixtures_are_skipped(tmp_path: Path) -> None:
    good = json.loads((E2E_REPLAYS / "tesla-advice.json").read_text("utf-8"))
    (tmp_path / "tesla-advice.json").write_text(json.dumps(good), "utf-8")
    (tmp_path / "renamed.json").write_text(json.dumps(good), "utf-8")  # slug != file name
    (tmp_path / "broken.json").write_text("{", "utf-8")
    assert list(load_replays(tmp_path)) == ["tesla-advice"]


def test_playback_compresses_long_waits_and_token_gaps() -> None:
    def ev(t: int, kind: str) -> RecordedEvent:
        raw = {"event": "token", "text": "x"} if kind == "token" else {
            "event": "node", "node": "planner", "phase": "start", "step_id": None,
            "error": False, "interrupted": False,
        }  # fmt: skip
        return RecordedEvent(t_ms=t, event=AGENT_EVENT.validate_python(raw))

    assert playback_delay_ms(None, ev(5_000, "node")) == 0
    assert playback_delay_ms(ev(0, "node"), ev(20_000, "node")) == 600  # 20 s tool call
    assert playback_delay_ms(ev(0, "node"), ev(400, "node")) == 100  # 4x faster
    assert playback_delay_ms(ev(0, "token"), ev(200, "token")) == 12
