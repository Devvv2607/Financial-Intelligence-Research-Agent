"""Record a run from a running API into a replay fixture (api/replays.py).

    uv run python -m api.replay_recorder --api https://<api-host> --slug nvda-ceo \\
        --question "Did the NVIDIA CEO resign?" [--answer "JPMorgan Chase (JPM)"] --yes

A live API spends real agent tokens (about 15K per run), so the run only starts with
`--yes`. The recording is labeled by what the API reports in `X-Agent-Runtime`: "live" or
"scripted" (the test runtime), never by a flag. It is refused, and nothing is written,
when the run ended in an error or the saved JSON contains anything that looks like an
API key, a bearer token or an IP address.
"""

import argparse
import json
import re
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx

from api.replays import SLUG_RE, RecordedEvent, Replay, Segment, SegmentInput
from api.schemas import AGENT_EVENT
from core.aio import run

_OCTET = r"(?:25[0-5]|2[0-4]\d|1?\d?\d)"
_HEX = r"[0-9A-Fa-f]{1,4}"
LEAK_PATTERNS = {
    "Groq API key": re.compile(r"gsk_[A-Za-z0-9]{8,}"),
    "API key": re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_-]{16,}"),
    "bearer token": re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{12,}"),
    "key parameter": re.compile(
        r"(?i)(?:api[_-]?key|apikey|token|secret)[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9]{12,}"
    ),
    "IPv4 address": re.compile(rf"(?<![\d.])(?:{_OCTET}\.){{3}}{_OCTET}(?![\d.])"),
    "IPv6 address": re.compile(rf"(?<![0-9A-Fa-f:])(?:{_HEX}:){{7}}{_HEX}(?![0-9A-Fa-f:])"),
}


def find_leaks(text: str) -> list[str]:
    """Names of the leak patterns found, with the first match shortened (for the error)."""
    hits = []
    for name, pattern in LEAK_PATTERNS.items():
        m = pattern.search(text)
        if m:
            hits.append(f"{name} ({m.group(0)[:12]}…)")
    return hits


async def _segment(
    client: httpx.AsyncClient, path: str, body: dict[str, str], kind: Literal["run", "resume"]
) -> Segment:
    events: list[RecordedEvent] = []
    t0 = time.monotonic()
    async with client.stream("POST", path, json=body) as resp:
        if resp.status_code != 200:
            await resp.aread()
            raise RuntimeError(f"{path} answered {resp.status_code}: {resp.text[:300]}")
        data: list[str] = []
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line and data:
                event = AGENT_EVENT.validate_python(json.loads("\n".join(data)))
                t_ms = int((time.monotonic() - t0) * 1000)
                events.append(RecordedEvent(t_ms=t_ms, event=event))
                data = []
    return Segment(
        input=SegmentInput(kind=kind, text=body.get("query") or body["answer"]), events=events
    )


async def record(
    client: httpx.AsyncClient,
    slug: str,
    question: str,
    answer: str | None = None,
    api_url: str | None = None,
) -> Replay:
    """Run `question` (and resume with `answer` if the agent asks) and return the replay.
    `client` must carry an X-Visitor-Id header (a fresh one per recording)."""
    ready = await client.get("/ready")
    source: Literal["live", "scripted"] = (
        "scripted" if ready.headers.get("x-agent-runtime") == "scripted" else "live"
    )
    thread = (await client.post("/api/threads", json={})).raise_for_status().json()["id"]
    segments = [await _segment(client, f"/api/threads/{thread}/runs", {"query": question}, "run")]
    asked = [e.event for e in segments[0].events if e.event.event == "clarification_needed"]
    if asked:
        if answer is None:
            raise RuntimeError(
                f"The agent asked for a clarification ({asked[0]!r}); pass --answer."
            )
        segments.append(
            await _segment(client, f"/api/threads/{thread}/resume", {"answer": answer}, "resume")
        )
    all_events = [e.event for s in segments for e in s.events]
    if not any(e.event == "final" for e in all_events):
        raise RuntimeError("The run did not end with a final answer; nothing recorded.")
    if any(e.event == "error" for e in all_events):
        raise RuntimeError("The run reported an error; nothing recorded.")
    run_id = next((e.run_id for e in all_events if e.event == "run_started"), None)
    return Replay(
        slug=slug,
        question=question,
        recorded_at=datetime.now(UTC),
        source=source,
        api_url=api_url,
        tokens=await _tokens(client, run_id),
        segments=segments,
    )


async def _tokens(client: httpx.AsyncClient, run_id: str | None) -> int | None:
    """Counted tokens of the run, from the API's own run log (None when unavailable)."""
    if run_id is None:
        return None
    try:
        r = await client.get(f"/api/runs/{run_id}")
        if r.status_code != 200:
            return None
        d: dict[str, Any] = r.json()
        if d.get("prompt_tokens") is None and d.get("completion_tokens") is None:
            return None
        return int(d.get("prompt_tokens") or 0) + int(d.get("completion_tokens") or 0)
    except (httpx.HTTPError, ValueError):
        return None


def render(replay: Replay) -> str:
    """The fixture file's text; refuses (ValueError) if it would leak a key or an IP."""
    text = json.dumps(replay.model_dump(mode="json"), indent=1, ensure_ascii=False) + "\n"
    leaks = find_leaks(text)
    if leaks:
        raise ValueError("Refusing to save the recording; it contains: " + ", ".join(leaks))
    return text


async def _main(args: argparse.Namespace) -> int:
    headers = {"X-Visitor-Id": str(uuid.uuid4())}  # a fresh visitor: nobody else sees it
    async with httpx.AsyncClient(base_url=args.api, timeout=300, headers=headers) as client:
        replay = await record(client, args.slug, args.question, args.answer, args.api)
    path = args.out / f"{args.slug}.json"
    args.out.mkdir(parents=True, exist_ok=True)
    path.write_text(render(replay), encoding="utf-8", newline="\n")
    events = sum(len(s.events) for s in replay.segments)
    print(f"wrote {path}: {replay.source} run, {events} events, tokens={replay.tokens}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m api.replay_recorder")
    parser.add_argument("--api", required=True, help="API base URL (public URL, no IPs)")
    parser.add_argument("--slug", required=True, help="file name: lower-case letters, digits, -")
    parser.add_argument("--question", required=True)
    parser.add_argument("--answer", help="clarification answer, if the agent asks")
    parser.add_argument("--out", type=Path, default=Path("replays"))
    parser.add_argument("--yes", action="store_true", help="confirm: a live API spends tokens")
    args = parser.parse_args(argv)
    if not SLUG_RE.match(args.slug):
        parser.error("--slug: lower-case letters, digits and '-', at most 63 characters")
    if find_leaks(args.api):
        parser.error("--api: use the public host name, not an IP address")
    if not args.yes:
        print("A live API spends about 15K agent tokens per run. Re-run with --yes to record.")
        return 2
    try:
        return run(_main(args))
    except (RuntimeError, ValueError, httpx.HTTPError) as exc:
        print(f"Not recorded: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
